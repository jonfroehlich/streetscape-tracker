"""
Fetch the frozen OSM street network for a city (issues #24/#103).

The network is fetched for the city's **frozen grid bounding box** (never
re-geocoded), so the streets line up exactly with the sampled grid the pano
runs use. Like frozen grid geometry, the network is a provider-agnostic city
asset (issue #103): fetched once, frozen to an unpublished GraphML cache under
``data/osm_cache/``, registered in the catalog's ``street_networks`` table,
and reused until ``--refresh`` (which replaces both the file and the catalog
row). GraphML (not GeoJSON) is used for the cache because it round-trips
osmnx's list-valued tags and is skipped by the publish whitelist (which only
ships ``*.csv.gz`` / ``*.json.gz``).
"""

from __future__ import annotations

import contextlib
import logging
import os
import random
import signal
import sqlite3
import threading
import time

import geopandas as gpd
import networkx as nx
import osmnx as ox
import pandas as pd
import requests
from osmnx._errors import InsufficientResponseError, ResponseStatusCodeError

from streetscape_metadata_tracker import db
from streetscape_metadata_tracker.db import CityRow
from streetscape_metadata_tracker.download_common import (
    HOST_OVERPASS,
    OVERPASS_REFERER,
    OVERPASS_URL_ENV,
    OVERPASS_USER_AGENT,
    DownloadError,
    HostBlockedError,
    grid_bbox,
)
from streetscape_metadata_tracker.host_lock import host_lock
from streetscape_metadata_tracker.naming import (
    DEFAULT_NETWORK_TYPE,
    network_cache_filename,
    network_cache_path,
    osm_cache_dir,
)
from streetscape_metadata_tracker.overpass_retry import (
    OVERPASS_RETRY_WINDOW_CEILING_S,
    OverpassRetryPolicy,
    RetriesExhausted,
    call_with_retry,
)

logger = logging.getLogger(__name__)

# Quiet by default; the CLI configures the root logger. use_cache also stores
# Overpass responses so re-fetches during development are cheap.
ox.settings.log_console = False
ox.settings.use_cache = True

# HTTP + server-side query timeout, in seconds.
#
# This deliberately KEEPS today's behaviour rather than restoring the 60 that
# was intended. `ox.settings.timeout = 60` lived here for a year and never did
# anything: osmnx 2.x (which requirements.txt pins on purpose) renamed the
# setting to `requests_timeout`, and `ox.settings` is a plain module, so the
# assignment silently created an attribute nothing reads. Every Overpass call
# has actually run at the 180 s default.
#
# Setting it to 60 now would be a real behaviour change, and a regression on
# exactly the cities most likely to need it: the SAME value is interpolated
# into the SERVER-side `[timeout:{}]` clause of the Overpass QL header
# (osmnx._overpass._make_overpass_settings), so 60 would start server-aborting
# large-bbox fetches that succeed today. See issue #209.
OVERPASS_TIMEOUT_S = 180
ox.settings.requests_timeout = OVERPASS_TIMEOUT_S

# osmnx's own politeness: it reads {overpass_url}/status and waits for a free
# slot before each uncached query. Pinned rather than assumed — it is an osmnx
# default we depend on, and a default can change under us. Note it is
# per-PROCESS, like every other limiter here, which is why the machine-wide
# host lock below is what actually bounds our aggregate rate (issue #208).
ox.settings.overpass_rate_limit = True

# The Overpass usage policy asks that clients "add User-Agent or Referer
# headers to requests that uniquely identify your app". osmnx's default names
# osmnx, which makes us indistinguishable from every other osmnx user on a
# shared volunteer-run instance. geoutils.py already does this for Nominatim;
# this is the same courtesy for Overpass, and it is what lets a maintainer
# contact us rather than simply firewalling the IP (issue #209). The strings
# live in download_common so the scheduler's osmnx-free breaker re-check
# (issue #341) sends the identical identity.
ox.settings.http_user_agent = OVERPASS_USER_AGENT
ox.settings.http_referer = OVERPASS_REFERER

# `OVERPASS_URL_ENV` is imported from download_common and re-exported here for
# the callers and tests that always read it from this module.


def _apply_overpass_url() -> None:
    """
    Point osmnx at ``$OVERPASS_URL`` if it is set, else leave its default.

    Read at CALL time rather than import time (and called again from
    ``fetch_graph``), for the same reason ``host_lock.lock_dir()`` is: this is
    the handle an operator reaches for at 03:00 during an incident, and an
    import-time read cannot be exercised by a test or changed without a
    restart. Idempotent, so calling it per fetch costs nothing.
    """
    override = os.environ.get(OVERPASS_URL_ENV)
    if override:
        ox.settings.overpass_url = override


_apply_overpass_url()

# Ceiling on one whole graph fetch, retries included.
#
# osmnx handles HTTP 429/504 by sleeping 55 s and recursing into itself with NO
# depth limit (osmnx/_overpass.py:477-486), and nothing configurable changes
# that. So a rate-limit-flavoured refusal never fails — it hangs until the
# scheduler's per-city timeout SIGKILLs the child, and a SIGKILL carries no
# exit code, so the #208 breaker can never learn what happened. A real 504 was
# observed from Overpass on 2026-08-15, so this path is live, not theoretical.
#
# Derived rather than a flat 15 minutes, because what it has to clear is the
# worst LEGITIMATE fetch: the longest retry window a policy may configure
# (issue #357; a retry is only started if its wait ends inside that window),
# then one full final attempt — a request timeout plus osmnx's own pre-request
# pause. That pause is the loose term — with `overpass_rate_limit = True`,
# `_get_overpass_pause` sleeps the entire slot wait the server advertises, so a
# busy instance can legitimately add minutes. 120 s of slack for it is the
# assumption being made here; exceeding it is reported as a refusal, which is
# the conservative direction (the night's other street channels are skipped, no
# city is blamed, and an alert names the host). If that turns out to fire on
# healthy-but-busy nights, raise the slack rather than removing the bound —
# unbounded is how the SIGKILL happens.
#
# Until #357 this read `3 * (OVERPASS_TIMEOUT_S + 120)`, "three tenacity
# attempts"; the value is still 900 s, which #357 kept as the outer limit on
# purpose, and a timing-out attempt that starts late in the window is now the
# case the slack covers rather than the third of three.
OVERPASS_ATTEMPT_SLACK_S = 120
OVERPASS_DEADLINE_S = int(
    OVERPASS_RETRY_WINDOW_CEILING_S + OVERPASS_TIMEOUT_S + OVERPASS_ATTEMPT_SLACK_S
)  # 900 s

# HTTP statuses from /status that mean "this instance is refusing this host".
#
# An ALLOW-list of refusals, not a deny-list of everything that isn't 200, and
# the asymmetry is the reason: a false negative costs one wasted fetch that
# produces the real error anyway, while a false positive skips every street
# channel of every city for the night. 502/503 from a front-end proxy while
# /interpreter is perfectly healthy is an ordinary thing on a volunteer-run
# instance, so anything not listed here means "can't tell — proceed".
#
# 509 is Bandwidth Limit Exceeded, which some Overpass front ends use for
# per-IP quota; 403 and 429 are the shapes actually seen from overpass-api.de.
_OVERPASS_REFUSAL_STATUSES = frozenset({403, 429, 509})

# Errors worth retrying: transport faults that a second attempt can plausibly
# fix. Everything else (a ban page, an empty bbox, a malformed response) is a
# settled answer, and retrying it just spends more requests to hear it again —
# which is precisely what we did into a host that had already refused us.
#
# Issue #357 widened HOW LONG these are retried (see `overpass_retry`), and
# deliberately not WHAT is: every member here waits on the same schedule,
# because a bare ECONNREFUSED is the signature of both a minutes-long flap and
# the 2026-08-14 abuse ban, and duration is the only discriminator (#341).
_RETRYABLE_OVERPASS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
)

# The retry loop's clock, sleep and jitter source, read at CALL time so a test
# (and conftest's suite-wide autouse stub) can substitute them — the suite must
# never really sleep out a multi-minute backoff. `time.sleep` is interruptible
# by the `_deadline` alarm, which is what keeps the window inside the deadline
# even if a policy's arithmetic were ever wrong.
_retry_sleep = time.sleep
_retry_clock = time.monotonic
_retry_random = random.random

NETWORK_TYPE = DEFAULT_NETWORK_TYPE

# `network_cache_filename` / `network_cache_path` moved to naming.py (the
# single source of truth for filenames) so the scheduler can test for a frozen
# network without importing this module's osmnx stack (issue #341). They are
# imported above and stay importable from here.
_cache_dir = osm_cache_dir


class _DeadlineExceeded(TimeoutError):
    """Raised by :func:`_deadline`'s alarm, and by nothing else.

    A distinct type rather than a bare ``TimeoutError`` because the builtin is
    also ``socket.timeout``: catching the builtin would let a stray socket
    timeout escaping urllib3 by a path ``requests`` didn't wrap be reported as
    "Overpass did not complete within 900s — most likely repeated 429/504",
    sending an operator after entirely the wrong thing.
    """


@contextlib.contextmanager
def _deadline(seconds: float):
    """
    Abort the wrapped block after ``seconds`` of wall clock.

    Exists solely to bound osmnx's unbounded 429/504 recursion (see
    OVERPASS_DEADLINE_S). SIGALRM is the only mechanism that can interrupt the
    ``time.sleep(55)`` at the bottom of that recursion — a cooperative check has
    nowhere to run, because the whole stack is inside third-party code.

    A no-op where SIGALRM can't be used: Windows, and any non-main thread
    (``signal.setitimer`` raises there). Losing the deadline is strictly better
    than breaking a caller who runs this off the main thread; the scheduler's
    per-city timeout remains the backstop it has always been.
    """
    if not hasattr(signal, "SIGALRM") or threading.current_thread() is not threading.main_thread():
        yield
        return

    def _fire(signum, frame):
        raise _DeadlineExceeded(f"Overpass fetch exceeded {seconds:g}s")

    previous = signal.signal(signal.SIGALRM, _fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _overpass_refusing(url: str | None = None) -> str | None:
    """
    Ask Overpass whether it will serve us, before we ask it to do real work.

    Returns a reason string when the instance is clearly unusable, else None.

    One cheap GET turns a per-IP block from "the night hangs for 18 minutes per
    city and dies by SIGKILL" into "named in about a second". The endpoint
    reports the caller's own rate-limit state, e.g.::

        Connected as: 403941390
        Rate limit: 2
        2 slots available now.

    Advisory ONLY, and the bar for saying "refusing" is deliberately high:
    anything unexpected — unreachable, unparseable, a format change, a 5xx from
    a front end — returns None and lets the real fetch proceed. A pre-flight
    that can fail a healthy collection is worse than no pre-flight (see the UA
    note below for how nearly that shipped), and this endpoint is not part of
    any documented contract.

    Deliberately NOT treated as a refusal: a queued slot. Overpass grants 2
    slots per IP and reports "Slot available after: <ts>, in N seconds" when
    both are in use — but osmnx reads the same endpoint in
    ``_overpass._get_overpass_pause`` and simply sleeps the wait off, so the
    fetch we would be cancelling was going to succeed. Being briefly queued
    behind our own previous query is the normal state of a working night.

    Sends osmnx's headers rather than plain ``requests`` defaults, and NOT as a
    nicety: overpass-api.de answers **HTTP 406** to the stock
    ``python-requests/x.y.z`` User-Agent (measured 2026-08-15 — every other UA
    tested returned 200). A probe using the default would have read that 406 as
    a refusal and skipped every city of every night. Reusing osmnx's builder
    also keeps the probe indistinguishable from the query it is speaking for,
    which is what the Overpass usage policy asks for.

    The whole body is guarded, not just the request. ``ox._http`` is a private
    osmnx API and ``requirements.txt`` pins ``osmnx>=2.0`` with no ceiling, so a
    rename would otherwise raise ``AttributeError`` from an advisory pre-flight
    — inside the host lock, before any real request, failing every street
    collection on the machine. Advisory means advisory.
    """
    try:
        base = (url or ox.settings.overpass_url).rstrip("/")
        response = requests.get(f"{base}/status", timeout=15, headers=ox._http._get_http_headers())
        if response.status_code in _OVERPASS_REFUSAL_STATUSES:
            return f"its status endpoint answered HTTP {response.status_code}"
    except Exception:  # noqa: BLE001 - advisory by contract; see docstring
        return None  # Can't tell. Let the real request produce the real error.
    return None


def _download_graph(bbox, network_type: str) -> nx.MultiDiGraph:
    """
    ONE attempt at downloading a simplified network for the bbox.

    No retry here: :func:`_download_graph_retrying` owns the schedule (issue
    #357). Before that this was wrapped in tenacity, whose default retried
    EVERYTHING and whose ``RetryError`` buried the cause — which is exactly why
    the 2026-08-14 alert emails said "RetryError" and never mentioned Overpass.

    A retry re-enters ``graph_from_bbox`` from the top, but only re-asks
    Overpass for sub-queries it has not yet answered: ``ox.settings.use_cache``
    stores every successful response, so a large bbox that osmnx split into
    several queries does not re-pay the parts that already landed.
    """
    # osmnx 2.x expects bbox=(left, bottom, right, top) == (min_lon, min_lat,
    # max_lon, max_lat), exactly what grid_bbox returns.
    return ox.graph_from_bbox(
        bbox=bbox,
        network_type=network_type,
        simplify=True,
        retain_all=True,
        truncate_by_edge=True,
    )


def _download_graph_retrying(
    bbox, network_type: str, policy: OverpassRetryPolicy
) -> nx.MultiDiGraph:
    """
    :func:`_download_graph`, retried on transport faults under ``policy``.

    Only ``_RETRYABLE_OVERPASS`` is retried; anything else propagates on the
    attempt that raised it. Exhaustion raises
    :class:`~streetscape_metadata_tracker.overpass_retry.RetriesExhausted`
    chained from the last fault.
    """
    return call_with_retry(
        lambda: _download_graph(bbox, network_type),
        policy,
        retryable=_RETRYABLE_OVERPASS,
        sleep=_retry_sleep,
        clock=_retry_clock,
        rand=_retry_random,
        what=f"Overpass ({ox.settings.overpass_url})",
    )


def _download_graph_named(
    bbox, network_type: str, retry_policy: OverpassRetryPolicy | None = None
) -> nx.MultiDiGraph:
    """
    ``_download_graph`` with a failure vocabulary, bounded in wall clock.

    Translates osmnx/requests exceptions into the shared types so a caller can
    act on them, mirroring what #199 did for Mapillary tiles: name the condition
    instead of letting it read as an anonymous traceback.

    The distinction that matters is host-wide vs. city-specific:

    * ``HostBlockedError`` — Overpass is refusing THIS MACHINE. Every remaining
      city tonight would fail identically, so the scheduler's breaker (#208)
      skips the rest rather than re-asking 19 more times.
    * plain ``DownloadError`` — this one bbox has no drivable ways, or came back
      malformed. A roadless village must NOT cancel the night's other cities,
      which is why InsufficientResponseError is deliberately not host-scoped.

    A connection refusal that outlasts the retry window (``retry_policy``,
    default :class:`OverpassRetryPolicy`; issue #357) is classified host-wide on
    purpose. A local network outage and a remote ban are indistinguishable from
    here and want the same action anyway (stop, blame no city, alert), so the
    message names both possibilities rather than guessing.

    **Known gap, deliberately not fixed here.** osmnx raises
    ``InsufficientResponseError`` from two unrelated situations: "the server
    returned no data elements" (genuinely about this bbox) *and* "the response
    was HTTP 200 but would not parse as JSON" (``_http._parse_response``, which
    picks that type over ``ResponseStatusCodeError`` precisely because the
    status was ok). So a captive portal, a middlebox interstitial, or an
    Overpass error page served with a 200 arrives here typed as a city failure
    and every city of the night re-asks — structurally the same bug #199 fixed
    for Mapillary tiles, where a 200 + ``text/html`` had been reading as a
    corrupt tile rather than a block. It is not fixed because osmnx does not
    hand the caller the response, so any fix would be a sniff of the exception
    message; the ``/status`` pre-flight above is the mitigation that actually
    catches the realistic version of this.
    """
    policy = OverpassRetryPolicy() if retry_policy is None else retry_policy
    try:
        with _deadline(OVERPASS_DEADLINE_S):
            return _download_graph_retrying(bbox, network_type, policy)
    except RetriesExhausted as e:
        raise HostBlockedError(
            f"Overpass ({ox.settings.overpass_url}) is unreachable from this host after "
            f"{e.attempts} attempt(s) over {e.elapsed_s:.0f} s: {e.last}. Either this "
            f"machine's IP is blocked — the limit is per-IP, not per-credential, so a "
            f"different key would not help — or the network is down, and the retry "
            f"window ([overpass] in the scheduler config, issue #357) was too short to "
            f"tell a flap from a ban. Check `curl {ox.settings.overpass_url}/status` FROM "
            f"THIS HOST; set {OVERPASS_URL_ENV} to a mirror to work around it (issue #209).",
            host=HOST_OVERPASS,
        ) from e.last
    except ResponseStatusCodeError as e:
        raise HostBlockedError(
            f"Overpass ({ox.settings.overpass_url}) refused this host: {e}. This is "
            f"scoped to the IP rather than to a credential; wait for it to lapse, or set "
            f"{OVERPASS_URL_ENV} to a mirror (issue #209).",
            host=HOST_OVERPASS,
        ) from e
    except _DeadlineExceeded as e:
        # Only reachable via _deadline: osmnx's 429/504 recursion never returns
        # on its own, so without this the child would be SIGKILLed with no exit
        # code and the breaker would never learn the host was refusing us.
        raise HostBlockedError(
            f"Overpass ({ox.settings.overpass_url}) did not complete within "
            f"{OVERPASS_DEADLINE_S}s: {e}. It answers but will not serve us — most "
            f"likely repeated 429/504 responses, which osmnx retries internally "
            f"forever (issue #209).",
            host=HOST_OVERPASS,
        ) from e
    except InsufficientResponseError as e:
        # City-specific by design: a bbox with no drivable ways is a real,
        # permanent answer about THIS city and must not trip the host breaker.
        raise DownloadError(
            f"Overpass returned no usable {network_type} network for bbox {bbox}: {e}. "
            f"This is about this city, not this host — a bbox genuinely without "
            f"{network_type} ways looks exactly like this."
        ) from e


def _register(
    conn: sqlite3.Connection, city_id: str, network_type: str, graph: nx.MultiDiGraph
) -> None:
    db.register_street_network(
        conn,
        city_id=city_id,
        graphml_filename=network_cache_filename(city_id, network_type),
        network_type=network_type,
        node_count=graph.number_of_nodes(),
        edge_count=graph.number_of_edges(),
        osmnx_version=ox.__version__,
    )


def fetch_graph(
    city_row: CityRow,
    data_dir: str,
    *,
    refresh: bool = False,
    network_type: str = NETWORK_TYPE,
    conn: sqlite3.Connection | None = None,
    overpass_retry: OverpassRetryPolicy | None = None,
) -> nx.MultiDiGraph:
    """
    Return the city's street graph, from the frozen cache or Overpass.

    The bbox comes from the frozen grid geometry via `grid_bbox`, so the
    network matches the sampled area. When a cache exists and ``refresh`` is
    False it is loaded; otherwise the graph is downloaded and cached.

    When ``conn`` is given, the frozen network is registered in the catalog's
    ``street_networks`` table (issue #103): a fresh download registers (or, on
    ``refresh``, replaces) the row, and a cache hit whose row is missing is
    backfilled — this adopts GraphML caches created before the catalog table
    existed, so their ``fetched_at``/``osmnx_version`` reflect load time, not
    the original fetch. Without ``conn`` the module works standalone,
    catalog-free (unit tests, ad-hoc use).

    ``overpass_retry`` is how long a cold fetch keeps asking a refusing
    Overpass before it gives up (issue #357); None means the defaults of
    :class:`OverpassRetryPolicy`. A cache hit never consults it.
    """
    # Keep osmnx's raw HTTP response cache inside the (unpublished) osm_cache
    # dir rather than a stray ./cache in the cwd.
    ox.settings.cache_folder = os.path.join(_cache_dir(data_dir), "osmnx")
    # Re-read $OVERPASS_URL: the incident-time mirror must be settable without
    # a restart, and a call-time read is the only version a test can exercise.
    _apply_overpass_url()

    cache_path = network_cache_path(city_row.city_id, data_dir, network_type)
    if not refresh and os.path.exists(cache_path):
        logger.info("Loading frozen street network from %s", cache_path)
        graph = ox.load_graphml(cache_path)
        if conn is not None and db.get_street_network(conn, city_row.city_id, network_type) is None:
            logger.info("Backfilling street_networks catalog row for %s", city_row.city_id)
            _register(conn, city_row.city_id, network_type, graph)
        return graph

    bbox = grid_bbox(
        city_row.center_lat,
        city_row.center_lon,
        city_row.grid_width_m,
        city_row.grid_height_m,
        city_row.step_m,
    )
    logger.info(
        "Downloading OSM %s network for %s within frozen bbox %s",
        network_type,
        city_row.city_id,
        bbox,
    )
    # Serialized machine-wide: Overpass meters by IP, so a second concurrent
    # process here is a hazard to the whole host (issue #208). Taken around the
    # whole retry stack rather than inside _download_graph, so a competing
    # process cannot slip in between our retries — and AFTER the cache-hit
    # return above, so a warm city never contends for it.
    with host_lock(HOST_OVERPASS):
        # Ask before working: one cheap GET names a refusal in ~1s instead of
        # after the whole retry window (issues #209, #357). Inside the lock so the
        # probe and the fetch see the same serialized world.
        #
        # This is a SECOND /status GET — osmnx makes its own before every query
        # (`_get_overpass_pause`) — and that duplication is the price of the
        # fast refusal, not an oversight. osmnx's call answers "how long until a
        # slot?" and treats everything else as a reason to pause and proceed;
        # ours answers "is this host refused?" and is the only thing that can
        # short-circuit. /status is unmetered, so the extra request costs
        # nothing. Do not "de-duplicate" these into one.
        refusing = _overpass_refusing()
        if refusing:
            raise HostBlockedError(
                f"Overpass ({ox.settings.overpass_url}) is not serving this host: "
                f"{refusing}. Skipping before issuing the query. The limit is per-IP, "
                f"so a different credential would not help; set {OVERPASS_URL_ENV} to "
                f"a mirror to work around it (issue #209).",
                host=HOST_OVERPASS,
            )
        # The whole retry window runs INSIDE the lock: a competing process
        # slipping in between our attempts would be a second talker against a
        # host that is already refusing this IP (issue #208), which is worse
        # than making it wait out the window as a busy exit.
        graph = _download_graph_named(bbox, network_type, overpass_retry)
    logger.info("Downloaded %d nodes / %d edges", graph.number_of_nodes(), graph.number_of_edges())

    os.makedirs(_cache_dir(data_dir), exist_ok=True)
    # Written beside the final name and renamed in, never in place (issue
    # #341): `ox.save_graphml` -> `nx.write_graphml` writes the file it is
    # given, so a Ctrl-C, SIGTERM or OOM mid-write used to leave a truncated
    # GraphML that every later reader took for a frozen network -- the cache
    # hit above never re-fetches, `ox.load_graphml` then fails as a plain city
    # failure every night, and the scheduler's frozen-network exemption trusts
    # the file's mere existence. `os.replace` is atomic on one filesystem, so
    # the final name either holds a complete network or nothing.
    tmp_path = cache_path + ".tmp"
    ox.save_graphml(graph, tmp_path)
    os.replace(tmp_path, cache_path)
    logger.info("Froze street network to %s", cache_path)
    if conn is not None:
        _register(conn, city_row.city_id, network_type, graph)
    return graph


def graph_to_edges(graph: nx.MultiDiGraph) -> gpd.GeoDataFrame:
    """
    Flatten a street graph to a WGS84 edge GeoDataFrame for coverage matching.

    Keeps one row per *undirected* edge with a stable ``edge_id`` (the unordered
    OSM node pair, e.g. ``"12_57"``), ``highway``, ``length`` (metres), and
    LineString ``geometry``. osmnx emits both directions of a two-way street as
    two directed edges; we collapse them by their unordered (u, v) node pair. We
    deliberately do NOT dedup on geometry WKB: osmnx orients each directed edge's
    geometry in its own travel direction, so the reciprocal edge's LineString is
    coordinate-reversed and its WKB differs — a WKB compare would keep both and
    double-count every two-way segment.

    ``edge_id`` is derived from OSM node IDs on the *frozen* network (issue
    #103), so it is stable across runs until a ``--refresh`` re-freezes the
    graph. The road-walk collector (issue #99) keys per-edge coverage on it, and
    it makes future run-to-run streetwalk diffs comparable.
    """
    edges = ox.graph_to_gdfs(graph, nodes=False)
    # graph_to_gdfs indexes edges by (u, v, key); collapse reciprocal directed
    # edges (v, u) onto (u, v) via an order-independent node-pair key.
    u = edges.index.get_level_values("u")
    v = edges.index.get_level_values("v")
    undirected_key = pd.Series(
        [frozenset((a, b)) for a, b in zip(u, v, strict=True)], index=edges.index
    )
    # Human-stable string form of the same unordered pair, order-independent so
    # both directions of a two-way street map to one id.
    edge_id = pd.Series(
        [f"{min(a, b)}_{max(a, b)}" for a, b in zip(u, v, strict=True)], index=edges.index
    )

    # `service` distinguishes an alley from a driveway or a parking aisle, all of
    # which are highway=service; without it the by-type breakdown cannot tell a
    # real back street from someone's driveway. It is already in
    # ox.settings.useful_tags_way, so retaining it needs no re-fetch — but a
    # network with no service roads (and any drive GraphML cached before this)
    # simply won't have the column, hence the membership guard.
    keep = [c for c in ("highway", "service", "length", "geometry") if c in edges.columns]
    edges = edges[keep].copy()
    edges["edge_id"] = edge_id
    edges = edges.loc[~undirected_key.duplicated()].reset_index(drop=True)
    return edges


def fetch_street_edges(
    city_row: CityRow,
    data_dir: str,
    *,
    refresh: bool = False,
    network_type: str = NETWORK_TYPE,
    conn: sqlite3.Connection | None = None,
    overpass_retry: OverpassRetryPolicy | None = None,
) -> gpd.GeoDataFrame:
    """Convenience wrapper: fetch the graph and return its edge GeoDataFrame."""
    graph = fetch_graph(
        city_row,
        data_dir,
        refresh=refresh,
        network_type=network_type,
        conn=conn,
        overpass_retry=overpass_retry,
    )
    return graph_to_edges(graph)
