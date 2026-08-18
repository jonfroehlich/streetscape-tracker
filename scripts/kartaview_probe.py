"""
Issue #225 feasibility probe: what can we actually get out of KartaView, and how fast?

    python scripts/kartaview_probe.py --targets                 # list built-in targets
    python scripts/kartaview_probe.py --area krabi              # one city, ~10 requests
    python scripts/kartaview_probe.py --area all --docs-dir docs/experiments

This is a PROBE, not a collector. It issues a handful of read-only requests per
target and writes a derived metrics record; it never sweeps a city, never writes
to data/, and never touches the catalog.

WHY A PROBE AND NOT A COLLECTOR (read before raising any limit here). KartaView
documents 100 requests/hour anonymous and 1,000/hour authenticated, and returns
NO rate-limit headers of any kind -- so there is no way to observe your remaining
budget, and the limit was measured as currently UNENFORCED (130 consecutive
requests, zero 429s). That combination is exactly CLAUDE.md's corollary: treat
undocumented-or-unenforced behavior as unknown rather than unlimited, and pace to
the published number regardless of the headroom you can see. See
docs/experiments/kartaview-feasibility.md.

THE API SHAPE IS NEITHER MAPILLARY'S NOR GSV'S. There is no metadata vector tile
endpoint; the coverage tiles carry geometry only and their .json/.geojson
variants return empty. The v2 /2.0/photo/ spatial query returns
`apiCode 408 "Query timeout"` for any unconstrained call. The only reliable
spatial path is POST /1.0/list/nearby-photos/ in RADIUS mode at r <= 300-500 m,
which is what this probe uses. Bbox mode errors or returns zero in the SOUTHERN
hemisphere -- where both of our target cities are -- so it is not used at all.

HTTP 400 IS BACKPRESSURE, NOT A MALFORMED REQUEST. The server signals overload
with HTTP 400 carrying `apiCode` 690 or 408. The correct response is to shrink
the radius and retry, which is the opposite of the usual 4xx reading and is the
easiest thing here to get wrong.

Derived metrics land in docs/experiments/ (committed, per CLAUDE.md); any bulk
sample dumps land in the gitignored experiments/kartaview/ -- NEVER under data/,
which the publisher rsyncs to a public web server.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from typing import Any

import requests
from dotenv import find_dotenv, load_dotenv

logger = logging.getLogger("kartaview_probe")

# The only reliable spatial endpoint (see module docstring). Radius mode only.
NEARBY_PHOTOS_URL = "https://kartaview.org/1.0/list/nearby-photos/"

# Documented ceilings, from the official FAQ (which is only reachable by
# scraping kartaview.org/main.*.js -- the docs are a JS SPA that returns nothing
# to a plain fetch) and corroborated by Bellingcat's toolkit entry. Neither was
# enforced when measured on 2026-08-18; both are honored anyway.
REQUESTS_PER_HOUR_ANON = 100
REQUESTS_PER_HOUR_AUTH = 1000

# Server-side page cap, from Grab's own JOSM plugin config
# (resources/kartaview_service.properties: nearbyPhotos.maxItems=2000).
IPP_MAX = 2000

# apiCode values that mean "you asked for too much" rather than "you asked
# wrongly". Both arrive inside an HTTP 400.
BACKPRESSURE_API_CODES = frozenset({408, 690})

# Radii to try, largest first. 300-500 m is the reported safe envelope; we start
# above it so the probe MEASURES the ceiling rather than assuming it.
RADIUS_LADDER_M = (1000, 500, 400, 300, 200, 100)

# Probe targets. Coordinates are the same ones the catalog froze for the two
# registered cities, so the probe measures the area we actually collect; the
# rest are comparison points named in issue #225 or found while checking it.
TARGETS: dict[str, dict[str, Any]] = {
    # --- registered in the catalog (issue #225, 2026-08-18) ---
    #
    # TWO Yogyakarta points on purpose, and the pair is the point. They sit ~1 km
    # apart and the 360 share between them swings 4% -> 100% (measured
    # 2026-08-18), because the Grab fleet's coverage is spatially concentrated
    # and the frozen grid centre lands just outside it. A single-point radius
    # probe is therefore a LOCAL estimate and never a city one — quoting one as a
    # city statistic is the easiest mistake this script enables.
    "yogyakarta": {
        "lat": -7.8033342,
        "lng": 110.37552685,
        "note": "frozen grid centre (what we actually collect); mostly community flat imagery",
    },
    "yogyakarta-malioboro": {
        "lat": -7.7956,
        "lng": 110.3695,
        "note": "Malioboro, ~1 km NW of the grid centre; the dense Grab KartaCam2 360 zone",
    },
    "krabi": {"lat": 8.0634637, "lng": 98.9162345, "note": "official Grab open-360 release city"},
    # --- already in the catalog, free comparison points ---
    "seattle": {"lat": 47.6097, "lng": -122.3331, "note": "our reference city; probed 90% SPHERE"},
    "singapore": {
        "lat": 1.2830,
        "lng": 103.8600,
        "note": "dense Grab 360 despite not being in the release",
    },
    "nyc": {"lat": 40.7580, "lng": -73.9855, "note": "probed 49% SPHERE"},
    # --- claims from issue #225 that the first pass could not confirm ---
    "langkawi": {
        "lat": 6.3200,
        "lng": 99.8500,
        "note": "in the official release; 0 photos at 5 probe points",
    },
    "bucharest": {"lat": 44.4360, "lng": 26.0910, "note": "claimed dense Telenav; probed 0% 360"},
}

DOCS_METRICS_NAME = "kartaview-feasibility_metrics.json"


def docs_generated_by(args: argparse.Namespace) -> str:
    """
    The command that actually produced the record, for ``_about.generated_by``.

    Spelled from the REAL arguments rather than a fixed constant, so a run cannot
    write a file claiming provenance it does not have -- CLAUDE.md requires the
    JSON a writeup cites to be regenerable by the command named inside it. Every
    flag that changes the record's CONTENT has to appear here: `--all-radii`
    decides whether per_radius exists at all, `--ipp` sets the denominator of
    every percentage, and `--repeat` changes how many entries a target gets.
    Mirrors ``pano_spacing_analyze.docs_generated_by``.
    """
    parts = ["scripts/kartaview_probe.py", "--area", str(args.area)]
    if args.all_radii:
        parts.append("--all-radii")
    if args.ipp != 200:
        parts += ["--ipp", str(args.ipp)]
    if args.repeat > 1:
        parts += ["--repeat", str(args.repeat)]
    parts += ["--docs-dir", str(args.docs_dir)]
    return " ".join(parts)


def refuse_on_collection_host() -> None:
    """
    Refuse to run on a production collection host.

    The whole point of this probe is to find a provider's limits by poking at
    them, and the one thing we must never do is find them with the nightly
    batch's IP. Both prior per-IP bans (Mapillary tiles #198, Overpass #209)
    landed on makelab2 and took out every channel on the box until they lifted.
    """
    host = socket.gethostname().lower()
    if host.startswith("makelab"):
        raise SystemExit(
            f"Refusing to probe KartaView from {host!r}: this is a production "
            f"collection host, and a per-IP limit found here would take out the "
            f"nightly batch. Run it from a laptop."
        )


class HourlyRateLimiter:
    """
    Paces requests to a documented PER-HOUR ceiling.

    Deliberately not ``download_common.AsyncRateLimiter``: that one is
    per-minute and async, and KartaView's published limit is an hourly figure
    with no per-minute component and no headers to observe. Spacing requests
    evenly across the hour (rather than bursting and sleeping) is the
    conservative reading of a limit whose enforcement window is unknown.
    """

    def __init__(self, requests_per_hour: int):
        self.min_interval_s = 3600.0 / max(1, requests_per_hour)
        self._last: float | None = None

    def acquire(self) -> None:
        if self._last is not None:
            wait = self.min_interval_s - (time.monotonic() - self._last)
            if wait > 0:
                logger.debug(f"pacing: sleeping {wait:.1f}s")
                time.sleep(wait)
        self._last = time.monotonic()


class ProbeError(RuntimeError):
    """A request failed in a way the caller should see rather than average away."""


def _post_nearby(
    session: requests.Session,
    limiter: HourlyRateLimiter,
    lat: float,
    lng: float,
    radius_m: int,
    page: int = 1,
    ipp: int = 200,
    access_token: str | None = None,
    timeout_s: float = 60.0,
) -> tuple[list[dict], int | None]:
    """
    One radius-mode nearby-photos call.

    Returns ``(items, total_filtered_items)``; the total is None when the server
    sent something we could not parse, which is deliberately distinct from 0.

    Raises:
        ProbeError: on a backpressure code (the caller shrinks the radius), on a
            transport failure, or on an unparseable body. Never returns an empty
            list to mean "failed" -- an empty result and a refused request are
            different facts, and conflating them is how a blocked host gets
            recorded as a city with no imagery.
    """
    limiter.acquire()
    params = {"access_token": access_token} if access_token else None
    data = {"lat": lat, "lng": lng, "radius": radius_m, "page": page, "ipp": min(ipp, IPP_MAX)}
    try:
        resp = session.post(NEARBY_PHOTOS_URL, data=data, params=params, timeout=timeout_s)
    except requests.RequestException as e:
        raise ProbeError(f"transport failure: {type(e).__name__}: {e}") from e

    try:
        body = resp.json()
    except ValueError as e:
        # An HTML error page on a 200 is how Mapillary's block manifested
        # (#199); name it rather than letting it reach a parser.
        ctype = resp.headers.get("Content-Type", "?")
        raise ProbeError(f"non-JSON body (HTTP {resp.status_code}, {ctype})") from e

    status = body.get("status") or {}
    api_code = status.get("apiCode")
    try:
        api_code = int(api_code)
    except (TypeError, ValueError):
        api_code = None

    if api_code in BACKPRESSURE_API_CODES:
        raise ProbeError(
            f"backpressure: apiCode {api_code} ({status.get('apiMessage', '')!r}) "
            f"at radius {radius_m} m"
        )
    if resp.status_code >= 400:
        raise ProbeError(f"HTTP {resp.status_code}, apiCode {api_code}, body keys {list(body)}")

    # The v1 envelope has varied across deployments; accept both shapes rather
    # than KeyError on a server that answered perfectly well.
    items = body.get("currentPageItems")
    if items is None:
        items = (body.get("osv") or {}).get("currentPageItems")
    if items is None:
        raise ProbeError(f"no currentPageItems in body (keys: {list(body)})")

    total = body.get("totalFilteredItems")
    if total is None:
        total = (body.get("osv") or {}).get("totalFilteredItems")
    # MEASURED 2026-08-18: the API returns this as a LIST HOLDING A STRING —
    # `['737']`, not `737`. A bare int() raises TypeError on that and would fall
    # back to len(items), silently reporting the page size (<= ipp) as the city
    # total: 5 instead of 737. That is the difference between "this city has
    # almost no imagery" and "this city is dense", i.e. exactly the number this
    # probe exists to produce, wrong in the direction that reads as a negative
    # result. Unwrap before coercing, and treat the fallback as unknown (None)
    # rather than as a count we did not actually measure.
    if isinstance(total, (list, tuple)):
        total = total[0] if total else None
    try:
        total = int(total)
    except (TypeError, ValueError):
        logger.warning(f"unparseable totalFilteredItems {total!r}; reporting as unknown")
        total = None

    return list(items), total


def _summarize(items: list[dict]) -> dict[str, Any]:
    """
    Tally the fields that decide whether KartaView is worth integrating.

    Every one of these is free in the bulk response -- no extra call -- which is
    the one respect in which this API is better than Mapillary's.
    """
    projections = Counter((it.get("projection") or "UNKNOWN") for it in items)
    n = len(items)

    # shot_date is contributor EXIF and is 100% NULL on the Grab 360 fleet
    # imagery; date_added is server-side upload time and can lag capture by
    # years. The null rate is the number that decides whether KartaView can
    # carry a temporal series at all, so it is measured, not assumed.
    shot_date_null = sum(1 for it in items if not it.get("shot_date"))

    # Tallied SEPARATELY BY SOURCE, never merged with a `shot_date or date_added`
    # fallback. Merging them publishes an upload year in the same field as a
    # capture year: Krabi's Grab imagery is 100% null shot_date and would read as
    # "2025" from its bulk-upload timestamp, sitting indistinguishably beside
    # Seattle's genuine 2025 capture year. For a project whose whole subject is
    # WHEN imagery was captured, that single conflation would invalidate every
    # temporal claim built on this record. A collector may still fall back to
    # date_added, but it has to say which it used.
    years_shot: Counter[str] = Counter()
    years_added: Counter[str] = Counter()
    for it in items:
        shot, added = it.get("shot_date") or "", it.get("date_added") or ""
        if len(shot) >= 4 and shot[:4].isdigit():
            years_shot[shot[:4]] += 1
        if len(added) >= 4 and added[:4].isdigit():
            years_added[added[:4]] += 1

    return {
        "n_sampled": n,
        "projection_counts": dict(projections),
        "pct_sphere": round(100.0 * projections.get("SPHERE", 0) / n, 2) if n else None,
        "shot_date_null": shot_date_null,
        "pct_shot_date_null": round(100.0 * shot_date_null / n, 2) if n else None,
        "distinct_sequences": len({it.get("sequence_id") for it in items if it.get("sequence_id")}),
        "distinct_usernames": len({it.get("username") for it in items if it.get("username")}),
        "usernames_top": dict(
            Counter(it.get("username") for it in items if it.get("username")).most_common(5)
        ),
        # capture years vs upload years, kept apart — see the comment above
        "capture_year_counts_shot_date": dict(sorted(years_shot.items())),
        "upload_year_counts_date_added": dict(sorted(years_added.items())),
    }


def probe_target(
    session: requests.Session,
    limiter: HourlyRateLimiter,
    name: str,
    lat: float,
    lng: float,
    ipp: int,
    access_token: str | None,
    all_radii: bool = False,
) -> dict[str, Any]:
    """
    Probe one target: walk the radius ladder down until a radius succeeds.

    The ladder is the measurement, not an implementation detail -- the largest
    radius that answers reliably is what sets the request count for a whole-city
    sweep, and therefore whether a nightly KartaView channel is affordable.

    With ``all_radii`` the ladder does NOT stop at the first success, and every
    rung's summary is recorded under ``per_radius``. That mode exists because
    pct_sphere turned out to be a deterministic function of radius (Yogyakarta's
    grid centre reads 4.0% / 7.5% / 14.5% at r=300/400/500), which is one of this
    study's main results -- and the default early-return records only the one
    rung that happened to win, leaving that series unreproducible from the
    committed record.
    """
    attempts: list[dict[str, Any]] = []
    per_radius: list[dict[str, Any]] = []
    first_ok: dict[str, Any] | None = None

    for radius in RADIUS_LADDER_M:
        try:
            items, total = _post_nearby(
                session, limiter, lat, lng, radius, ipp=ipp, access_token=access_token
            )
        except ProbeError as e:
            logger.info(f"{name}: r={radius}m FAILED - {e}")
            attempts.append({"radius_m": radius, "ok": False, "error": str(e)})
            if all_radii:
                per_radius.append({"radius_m": radius, "ok": False, "error": str(e)})
            continue

        logger.info(f"{name}: r={radius}m ok - {total} total, {len(items)} sampled")
        attempts.append(
            {
                "radius_m": radius,
                "ok": True,
                "total_filtered_items": total,
                "n_returned": len(items),
            }
        )
        summary = _summarize(items)
        if all_radii:
            per_radius.append(
                {"radius_m": radius, "ok": True, "total_filtered_items": total, **summary}
            )
            if first_ok is None:
                first_ok = {"radius": radius, "total": total, "summary": summary}
            continue

        return {
            "target": name,
            "lat": lat,
            "lng": lng,
            "max_working_radius_m": radius,
            "total_filtered_items": total,
            "attempts": attempts,
            **_summarize(items),
        }

    if all_radii:
        # Report the LARGEST working rung as the headline (the ladder runs
        # largest-first, so that is the first success), with every rung kept
        # alongside it so the radius-dependence is reproducible.
        head = first_ok or {"radius": None, "total": None, "summary": {"n_sampled": 0}}
        return {
            "target": name,
            "lat": lat,
            "lng": lng,
            "max_working_radius_m": head["radius"],
            "total_filtered_items": head["total"],
            "attempts": attempts,
            "per_radius": per_radius,
            **head["summary"],
        }

    return {
        "target": name,
        "lat": lat,
        "lng": lng,
        "max_working_radius_m": None,
        "total_filtered_items": None,
        "attempts": attempts,
        "n_sampled": 0,
        "note": "every radius failed; this is NOT the same as 'no imagery here'",
    }


def write_docs_record(results: list[dict], args: argparse.Namespace, authed: bool) -> str:
    """Write the committed metrics record beside the writeup. Sole producer."""
    os.makedirs(args.docs_dir, exist_ok=True)
    path = os.path.join(args.docs_dir, DOCS_METRICS_NAME)
    payload = {
        "_about": {
            "experiment": "kartaview-feasibility",
            "writeup": "docs/experiments/kartaview-feasibility.md",
            "generated_by": docs_generated_by(args),
            "issue": 225,
            "probed_at_utc": datetime.now(UTC).isoformat(),
            "authenticated": authed,
            "rate_limit_used_per_hour": REQUESTS_PER_HOUR_AUTH
            if authed
            else REQUESTS_PER_HOUR_ANON,
            "note": (
                "Feasibility probe for adding KartaView as a third provider. Radius-mode "
                "/1.0/list/nearby-photos/ only -- there is no metadata tile endpoint and the v2 "
                "spatial query returns apiCode 408. Percentages are over the SAMPLED page "
                "(<= ipp), not over total_filtered_items, so pct_sphere is an estimate of the "
                "local mix rather than a census. A target where every radius failed is recorded "
                "with max_working_radius_m = null and is NOT evidence of absent imagery."
            ),
        },
        "targets": results,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=False)
        f.write("\n")
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="KartaView feasibility probe (issue #225).")
    p.add_argument(
        "--area", default="all", help=f"target to probe, or 'all' (known: {', '.join(TARGETS)})"
    )
    p.add_argument("--targets", action="store_true", help="list the built-in targets and exit")
    p.add_argument(
        "--ipp",
        type=int,
        default=200,
        help=f"items per page to sample (server cap {IPP_MAX}; default 200)",
    )
    p.add_argument(
        "--all-radii",
        action="store_true",
        help=(
            "probe EVERY rung of the radius ladder instead of stopping at the first "
            "success, recording each under per_radius. pct_sphere is a deterministic "
            "function of radius, so this is what makes that series reproducible."
        ),
    )
    p.add_argument(
        "--repeat",
        type=int,
        default=1,
        help=(
            "probe each target this many times. The working radius is NOT stable "
            "run-to-run (transient apiCode 690s), and a different radius returns a "
            "different sample, so one run's pct_sphere is not reproducible. Use >1 "
            "to measure that spread."
        ),
    )
    p.add_argument(
        "--docs-dir",
        default=None,
        help="write the committed metrics record here (e.g. docs/experiments)",
    )
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")

    if args.targets:
        for name, t in TARGETS.items():
            print(f"{name:12s} {t['lat']:>10.5f}, {t['lng']:>11.5f}   {t['note']}")
        return 0

    refuse_on_collection_host()

    if args.area == "all":
        selected = list(TARGETS)
    elif args.area in TARGETS:
        selected = [args.area]
    else:
        # Exit 64 (EX_USAGE), matching scheduler.USAGE_EXIT_CODE, so a wrapper
        # can tell a mistyped target from a genuine probe failure.
        print(f"Unknown --area {args.area!r} (known: {', '.join(TARGETS)}, all)", file=sys.stderr)
        return 64

    # usecwd=True so the project's own .env wins over any higher-up one — dotenv
    # walks UP from the start dir and stops at the first hit, so a stray ~/.env
    # would otherwise be found only when the project has none.
    load_dotenv(find_dotenv(usecwd=True))
    access_token = os.environ.get("KARTAVIEW_ACCESS_TOKEN") or None
    authed = access_token is not None
    limiter = HourlyRateLimiter(REQUESTS_PER_HOUR_AUTH if authed else REQUESTS_PER_HOUR_ANON)
    logger.info(
        f"{'authenticated' if authed else 'ANONYMOUS'} probe, paced at "
        f"{REQUESTS_PER_HOUR_AUTH if authed else REQUESTS_PER_HOUR_ANON} req/hr "
        f"({limiter.min_interval_s:.1f}s between requests)"
    )

    session = requests.Session()
    session.headers.update(
        {"User-Agent": "streetscape-tracker probe (github.com/jonfroehlich/streetscape-tracker)"}
    )

    results = []
    for name in selected:
        t = TARGETS[name]
        for rep in range(max(1, args.repeat)):
            r = probe_target(
                session,
                limiter,
                name,
                t["lat"],
                t["lng"],
                args.ipp,
                access_token,
                all_radii=args.all_radii,
            )
            # MEASURED 2026-08-18: two back-to-back full runs disagreed. The
            # apiCode 690 failures are transient and load-dependent rather than a
            # fixed function of radius, so a different radius wins on each run,
            # which returns a different page of photos, which moves pct_sphere
            # (NYC read 48.5% then 100%). --repeat exists so that instability is
            # something the record MEASURES rather than something a single run
            # hides. Quote a range from repeats, never one run's number.
            if args.repeat > 1:
                r["repeat_index"] = rep
            results.append(r)

    print(
        f"\n{'target':12s} {'r_m':>5s} {'total':>8s} {'n':>5s} {'%SPHERE':>8s} {'%null date':>11s}"
    )
    for r in results:
        print(
            f"{r['target']:12s} {str(r['max_working_radius_m'] or '-'):>5s} "
            f"{str(r['total_filtered_items'] if r['total_filtered_items'] is not None else '-'):>8s} "
            f"{r['n_sampled']:>5d} {str(r.get('pct_sphere', '-')):>8s} "
            f"{str(r.get('pct_shot_date_null', '-')):>11s}"
        )

    if args.docs_dir:
        print(f"\nWrote {write_docs_record(results, args, authed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
