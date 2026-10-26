"""
``scheduler probe-host``: ask a per-IP host ONE question (issue #444).

The recovery recipe for a per-IP block has been the same since August: full
silence, then a single request, and read the answer. Until this module that
request was an untracked shell script on the production host, which made two
requests rather than one and recorded nothing. Here it is one request, made
under the same machine-wide host lock every collection child takes, with its
verdict written to the catalog's ``host_refusals`` table -- so the cross-night
refusal cooldown (``[hosts.<token>].refusal_cooldown_h``) and ``scheduler
status`` see it.

This module, not ``scheduler.py``, takes the host lock. The scheduler PARENT
must never hold one, because ``flock`` is not inherited across
``subprocess.run`` and a parent holding it would make every child it spawns
fail (issue #208; ``tests/test_host_lock.py`` pins that ``scheduler.py`` never
names it). ``probe-host`` is a separate operator command that spawns no child,
so holding the lock for its one request is exactly the serialization the lock
exists for.

The request is injectable (``fetch``) so the suite never reaches the network.

Example::

    python -m streetscape_metadata_tracker.scheduler \\
        --config config/scheduler.makelab1.toml probe-host mapillary_tiles
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from .download_common import (
    HOST_BUSY_EXIT_CODES,
    HOST_EXIT_CODES,
    HOST_MAPILLARY_TILES,
    redact_credentials,
)
from .host_lock import host_lock

logger = logging.getLogger("streetscape_scheduler")

# The one tile the Mapillary probe asks for: z14 (the only zoom whose tiles carry
# per-image metadata, download_mapillary.TILE_ZOOM) x=2581, y=5921 -- downtown
# Corvallis, Oregon and the Oregon State campus, lon -123.2886..-123.2666, lat
# 44.5592..44.5748. Chosen as KNOWN-POPULATED from this project's own data: the
# 2026-07-06 Corvallis Mapillary census held 269 distinct images in it, the
# densest tile of that city. Population is not what the verdict turns on (the
# block is a 302 to a login page whatever the tile holds), but a populated tile
# makes a 200 a real tile rather than an empty one, so a CLEARED verdict means
# the CDN served us data.
MAPILLARY_PROBE_TILE = (14, 2581, 5921)

# Exit statuses: cleared, refused (the host's own blocked code, as a collection
# child would exit), busy locally (the host's busy code), inconclusive.
PROBE_CLEARED_EXIT = 0
PROBE_INCONCLUSIVE_EXIT = 1

# A fetch primitive: url -> (HTTP status, headers). It must NOT follow redirects
# -- a 302 to /login IS the answer, and following it lands on the login page's
# own 200 (the exact failure #199 fixed in the collector).
Fetch = Callable[[str], tuple[int, dict[str, str]]]

PROBE_TIMEOUT_S = 30


@dataclass(frozen=True)
class ProbeResult:
    """One probe's answer. ``verdict`` is ``db.HOST_REFUSED``/``HOST_CLEARED`` or None."""

    verdict: str | None
    status: int | None
    detail: str


def probe_headers() -> dict[str, str]:
    """
    The probe's request headers: the collector's exact User-Agent (issue #444).

    The probe exists to predict what tonight's collector will be told, so it
    must look like the collector. It sends ``MAPILLARY_USER_AGENT`` verbatim --
    aiohttp library token included, although this request goes out through
    ``requests`` -- because a CDN that scores the User-Agent would otherwise
    be asked about a client it never sees at night (``requests``' default is
    ``python-requests/<ver>``).
    """
    from .download_mapillary import MAPILLARY_USER_AGENT

    return {"User-Agent": MAPILLARY_USER_AGENT}


def requests_fetch(url: str) -> tuple[int, dict[str, str]]:
    """The production fetch: one GET, redirects NOT followed, body not read."""
    import requests

    with requests.get(
        url,
        headers=probe_headers(),
        allow_redirects=False,
        timeout=PROBE_TIMEOUT_S,
        stream=True,
    ) as r:
        return r.status_code, dict(r.headers)


def mapillary_probe_url(token: str) -> str:
    """The probe tile's URL, from the collector's own endpoint template."""
    from .download_mapillary import TILE_URL_TEMPLATE

    z, x, y = MAPILLARY_PROBE_TILE
    return f"{TILE_URL_TEMPLATE.format(z=z, x=x, y=y)}?access_token={token}"


def classify_mapillary(status: int, headers: dict[str, str]) -> ProbeResult:
    """Read one tile response as the collector reads it (``_fetch_tile``).

    * a redirect the collector treats as the block (``TILE_REDIRECT_STATUSES``;
      the block is a 302 to ``www.mapillary.com/login``) or a 200 carrying an
      HTML/JSON error page: REFUSED. A 304 is not one;
    * a status in the collector's ``TILE_BACKPRESSURE_STATUSES`` (429, 503):
      THROTTLED, which is INCONCLUSIVE and recorded nowhere. The collector
      reads both as BACKPRESSURE -- it waits, slows down and at worst stops
      THROTTLED (exit 87), recording no refusal -- so the probe must not read
      them as a refusal either: a refused row would arm the 36 h
      cooldown on an answer the collector itself would have waited out. The
      ``Retry-After`` is printed when the CDN sends one;
    * a 200 tile, or a 404 (the CDN answered, with no tile): CLEARED;
    * a 401/403 (the TOKEN was rejected, a per-credential fault, not a
      per-IP one), any 5xx, anything else: INCONCLUSIVE, recorded nowhere.
    """
    from . import db
    from .download_mapillary import (
        _TILE_ERROR_CONTENT_TYPES,
        TILE_BACKPRESSURE_STATUSES,
        TILE_REDIRECT_STATUSES,
    )

    lowered = {k.lower(): v for k, v in headers.items()}
    if status in TILE_REDIRECT_STATUSES:
        location = redact_credentials(lowered.get("location", "(none)"))
        return ProbeResult(db.HOST_REFUSED, status, f"HTTP {status} -> {location}")
    if status in TILE_BACKPRESSURE_STATUSES:
        retry = lowered.get("retry-after")
        return ProbeResult(
            None,
            status,
            f"HTTP {status}: throttled (backpressure, not a refusal)"
            + (f"; Retry-After: {retry}" if retry else "; no Retry-After"),
        )
    if status == 200:
        content_type = lowered.get("content-type", "")
        if any(bad in content_type.lower() for bad in _TILE_ERROR_CONTENT_TYPES):
            return ProbeResult(
                db.HOST_REFUSED, status, f"HTTP 200 error page (Content-Type: {content_type})"
            )
        return ProbeResult(db.HOST_CLEARED, status, f"HTTP 200 tile ({content_type or 'no type'})")
    if status == 404:
        return ProbeResult(db.HOST_CLEARED, status, "HTTP 404 (served, no tile)")
    if status in (401, 403):
        return ProbeResult(
            None, status, f"HTTP {status}: the token was rejected; check MAPILLARY_ACCESS_TOKEN"
        )
    return ProbeResult(None, status, f"HTTP {status}: not a verdict either way")


def load_mapillary_token() -> str:
    """``MAPILLARY_ACCESS_TOKEN`` through the existing loader, after ``.env``.

    Raises ``ValueError`` (the loader's own message) when it is unset.
    """
    from dotenv import find_dotenv, load_dotenv

    from . import config

    load_dotenv()
    config.warn_if_credentials_world_readable(find_dotenv(usecwd=True))
    return config.load_config("mapillary")["access_token"]


# host -> (token loader, url builder, classifier). One entry per host the
# command can probe; Mapillary's tile CDN is the only one asked for (#444).
PROBES: dict[str, tuple[Callable[[], str], Callable[[str], str], Callable]] = {
    HOST_MAPILLARY_TILES: (load_mapillary_token, mapillary_probe_url, classify_mapillary),
}


def probe(host: str, token: str, fetch: Fetch) -> tuple[ProbeResult, bool]:
    """Send the ONE request under the host lock; return ``(result, sent)``.

    ``sent`` is False only when no HTTP response came back (a transport
    failure), which is the one case the request may never have reached the
    host. Raises ``download_common.HostBusyError`` when another local process
    holds the lock.
    """
    _token_loader, url_for, classify = PROBES[host]
    url = url_for(token)
    with host_lock(host):
        try:
            status, headers = fetch(url)
        except Exception as exc:  # noqa: BLE001 - a transport failure is "inconclusive"
            return (
                ProbeResult(None, None, f"no response: {redact_credentials(str(exc))}"),
                False,
            )
    return classify(status, headers), True


def exit_code_for(host: str, result: ProbeResult) -> int:
    """The command's exit status for ``result``."""
    from . import db

    if result.verdict == db.HOST_REFUSED:
        return HOST_EXIT_CODES[host]
    if result.verdict == db.HOST_CLEARED:
        return PROBE_CLEARED_EXIT
    return PROBE_INCONCLUSIVE_EXIT


def busy_exit_code(host: str) -> int:
    """The command's exit status when another local process holds the host lock."""
    return HOST_BUSY_EXIT_CODES[host]
