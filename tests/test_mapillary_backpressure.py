"""
The Mapillary tile census's view of the CDN's answers (issue #444).

Three things live here, each driven through the REAL ``_fetch_tile`` over an
in-memory ``aiohttp.ClientSession`` stand-in, never HTTP mocking and never the
network:

* the per-crawl status line (stage 2): every response counted by class, with
  retries and any ``Retry-After``, logged once per crawl on every way out;
* the identifying ``User-Agent`` (stage 4), pinned at the session's
  construction site rather than as a constant;
* backpressure on 429/503 (stage 4): wait, halve the pace, pause after three.

``_stub_fetch_tile`` (tests/test_mapillary.py) replaces ``_fetch_tile`` itself,
so nothing here uses it: a stub has no HTTP status to count.
"""

import asyncio
import logging
import re

import aiohttp
import mapbox_vector_tile
import pytest
import yarl
from multidict import CIMultiDict, CIMultiDictProxy

from streetscape_metadata_tracker import download_mapillary as dm
from streetscape_metadata_tracker.download_common import (
    DownloadError,
    HostBlockedError,
    SweepIncompleteError,
)

# A Chicago frame of 361 z14 tiles: big enough that one failed tile sits under
# MAX_FAILED_TILE_FRACTION (so a per-tile failure is tolerated, not a refusal).
_LAT, _LON = 41.8, -87.7
_BBOX = dm.grid_bbox(_LAT, _LON, 30000, 30000, 2000)
_TILES = dm.tiles_for_bbox(*_BBOX)
_EMPTY_TILE = mapbox_vector_tile.encode([])
_URL_TILE = re.compile(r"/2/14/(\d+)/(\d+)\?access_token=")


class _Response:
    """An aiohttp response stand-in: status, headers, body, raise_for_status."""

    def __init__(self, status, headers=None, body=_EMPTY_TILE):
        self.status = status
        self.headers = headers or {}
        self._body = body

    async def read(self):
        return self._body

    def raise_for_status(self):
        if self.status >= 400:
            url = yarl.URL("https://tiles.mapillary.com/x")
            info = aiohttp.RequestInfo(
                url=url, method="GET", headers=CIMultiDictProxy(CIMultiDict()), real_url=url
            )
            raise aiohttp.ClientResponseError(
                request_info=info, history=(), status=self.status, message="error"
            )


def ok():
    return _Response(200, {"Content-Type": "application/x-protobuf"})


def status(code, **headers):
    return _Response(code, headers)


class _ScriptedSession:
    """
    ``aiohttp.ClientSession`` stand-in answering each tile from a script.

    ``script`` maps (x, y) to a list of responses served in order, one per
    attempt; a tile not in it (or past the end of its list) gets a 200 empty
    tile. Records the kwargs it was BUILT with, and every URL it was asked for.
    """

    def __init__(self, script, **kwargs):
        self._script = {xy: list(rs) for xy, rs in script.items()}
        self.kwargs = kwargs
        self.urls: list[str] = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        m = _URL_TILE.search(url)
        assert m, url
        queue = self._script.get((int(m.group(1)), int(m.group(2))))
        response = queue.pop(0) if queue else ok()

        class _Ctx:
            async def __aenter__(self):
                return response

            async def __aexit__(self, *exc):
                return False

        return _Ctx()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def no_backoff_sleep(monkeypatch):
    """Make backoff's retry sleeps instant; returns the delays it asked for.

    ``_fetch_tile``'s ``@backoff`` decorator awaits ``asyncio.sleep`` between
    attempts (up to seconds each). Nothing else in a crawl over a fake session
    sleeps -- conftest disables the pacer -- so a global patch is contained.
    """
    real_sleep = asyncio.sleep
    delays = []

    async def instant(delay, *args, **kwargs):
        delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", instant)
    return delays


def _crawl(monkeypatch, script, **kwargs):
    """Run one census over a scripted session. Returns (result-or-error, session)."""
    sessions = []

    def build(**session_kwargs):
        session = _ScriptedSession(script, **session_kwargs)
        sessions.append(session)
        return session

    monkeypatch.setattr(dm.aiohttp, "ClientSession", build)
    try:
        outcome = asyncio.run(
            dm.fetch_city_images_async("Test City", _BBOX, "MLY|test|token", **kwargs)
        )
    except DownloadError as e:
        outcome = e
    return outcome, sessions[0]


def _status_lines(caplog):
    return [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith(dm.TILE_STATUS_LINE_PREFIX)
    ]


# ── Stage 2: the per-crawl status line ─────────────────────────────────────


def test_every_status_lands_in_its_class():
    assert [dm.tile_status_class(s) for s in (200, 204, 404, 429)] == ["200", "204", "404", "429"]
    assert [dm.tile_status_class(s) for s in (301, 302, 307, 308)] == ["3xx"] * 4
    assert [dm.tile_status_class(s) for s in (400, 401, 403, 410)] == ["4xx"] * 4
    assert [dm.tile_status_class(s) for s in (500, 502, 503, 504)] == ["5xx"] * 4
    assert dm.tile_status_class(101) == "other"
    assert set(dm.TILE_STATUS_CLASSES) == {
        "200",
        "204",
        "404",
        "429",
        "4xx",
        "5xx",
        "3xx",
        "other",
    }


def test_a_crawl_logs_one_exact_status_line(monkeypatch, caplog, no_backoff_sleep):
    """Every response counted once, by class, with retries and Retry-After.

    The 500 and the 404s are retried by ``@backoff``; the 404 tile exhausts
    its tries and is one tolerated failed tile, so the crawl still SUCCEEDS --
    and its line must still say what the CDN answered along the way.
    """
    a, b, c = _TILES[0], _TILES[1], _TILES[2]
    script = {
        a: [status(500, **{"Retry-After": "7"}), ok()],
        b: [status(404)] * dm.TILE_MAX_TRIES,
        c: [status(204, **{"Content-Type": "application/x-protobuf"})],
    }
    caplog.set_level(logging.INFO, logger=dm.__name__)
    result, _ = _crawl(monkeypatch, script)

    assert not isinstance(result, BaseException), result
    assert result["failed_tiles"] == [b]
    requests = len(_TILES) + 1 + (dm.TILE_MAX_TRIES - 1)
    assert result["api_requests"] == requests
    assert _status_lines(caplog) == [
        f"mapillary tile statuses: 200={len(_TILES) - 2} 204=1 404={dm.TILE_MAX_TRIES} "
        f"429=0 4xx=0 5xx=1 3xx=0 other=0 noresp=0 requests={requests} "
        f"retries={1 + dm.TILE_MAX_TRIES - 1} retry_after=[7] city='Test City'"
    ]


def test_a_request_with_no_response_is_counted_as_noresp(monkeypatch, caplog, no_backoff_sleep):
    """A timeout has no status, so it is `requests` minus the statuses."""

    class _Raises:
        async def __aenter__(self):
            raise aiohttp.ClientConnectionError("reset")

        async def __aexit__(self, *exc):
            return False

    first = _TILES[0]
    served = {"n": 0}
    real_get = _ScriptedSession.get

    def get(self, url, **kwargs):
        if f"/{first[0]}/{first[1]}?" in url and served["n"] == 0:
            served["n"] += 1
            self.urls.append(url)
            return _Raises()
        return real_get(self, url, **kwargs)

    monkeypatch.setattr(_ScriptedSession, "get", get)
    caplog.set_level(logging.INFO, logger=dm.__name__)
    result, _ = _crawl(monkeypatch, {})

    assert result["api_requests"] == len(_TILES) + 1
    (line,) = _status_lines(caplog)
    assert f"200={len(_TILES)} " in line
    assert " noresp=1 " in line
    assert " retries=1 " in line


def test_a_refused_crawl_still_logs_its_statuses(monkeypatch, caplog):
    """The night the line matters most: the 302 -> /login refusal, exit 75.

    The refusal is counted BEFORE ``_fetch_tile`` raises it, and the line is
    logged although the crawl ends in a HostBlockedError.
    """
    login = {"Location": "https://www.mapillary.com/login/?next=x%3Faccess_token%3DMLY"}
    script = {xy: [status(302, **login)] for xy in _TILES}
    caplog.set_level(logging.INFO, logger=dm.__name__)
    error, _ = _crawl(monkeypatch, script, connection_limit=1)

    assert isinstance(error, HostBlockedError)
    assert _status_lines(caplog) == [
        "mapillary tile statuses: 200=0 204=0 404=0 429=0 4xx=0 5xx=0 3xx=1 other=0 "
        "noresp=0 requests=1 retries=0 retry_after=[] city='Test City'"
    ]


def test_a_paused_crawl_still_logs_its_statuses(monkeypatch, caplog, tmp_path):
    """A request-cap pause (exit 83) ends the crawl early; the line still lands."""
    caplog.set_level(logging.INFO, logger=dm.__name__)
    error, _ = _crawl(
        monkeypatch,
        {},
        connection_limit=1,
        max_requests=3,
        checkpoint_path=str(tmp_path / "cp"),
    )

    assert isinstance(error, SweepIncompleteError)
    (line,) = _status_lines(caplog)
    assert line.startswith("mapillary tile statuses: 200=3 204=0 ")
    assert " requests=3 retries=0 " in line


def test_retry_after_values_are_capped_in_the_line_but_counted():
    counts = dm.TileStatusCounts()
    for i in range(dm.TILE_RETRY_AFTER_LOG_CAP + 5):
        counts.record(429, str(i))
    line = counts.summary_line("X", dm.TILE_RETRY_AFTER_LOG_CAP + 5)
    shown = ",".join(str(i) for i in range(dm.TILE_RETRY_AFTER_LOG_CAP))
    assert f"retry_after=[{shown},+5 more]" in line
    assert f"429={dm.TILE_RETRY_AFTER_LOG_CAP + 5} " in line
