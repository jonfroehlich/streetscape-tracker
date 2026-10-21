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
import json
import logging
import os
import re
import time

import aiohttp
import mapbox_vector_tile
import pytest
import yarl
from multidict import CIMultiDict, CIMultiDictProxy

from streetscape_metadata_tracker import download_mapillary as dm
from streetscape_metadata_tracker.download_common import (
    HOST_MAPILLARY_TILES,
    HOST_THROTTLED_EXIT_CODES,
    SWEEP_STOP_BACKPRESSURE,
    SWEEP_STOP_PHRASES,
    DownloadError,
    HostBlockedError,
    HostThrottledError,
    SweepIncompleteError,
    host_exit_code,
)

_THROTTLED = HOST_THROTTLED_EXIT_CODES[HOST_MAPILLARY_TILES]

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

    def __init__(self, script, overlap=False, storm_from=(), **kwargs):
        self._script = {xy: list(rs) for xy, rs in script.items()}
        # `overlap` yields once before answering, so requests on several
        # sockets are in flight together -- without it every response lands
        # before the next request goes out, and five sockets act as one.
        self.overlap = overlap
        # Once any tile in `storm_from` is asked for, EVERY request answers
        # 503 from then on: a host-wide storm beginning at that tile.
        self._storm_from = set(storm_from)
        self._storming = False
        self.kwargs = kwargs
        self.urls: list[str] = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        m = _URL_TILE.search(url)
        assert m, url
        tile = (int(m.group(1)), int(m.group(2)))
        self._storming = self._storming or tile in self._storm_from
        queue = self._script.get(tile)
        response = status(503) if self._storming else (queue.pop(0) if queue else ok())
        overlap = self.overlap

        class _Ctx:
            async def __aenter__(self):
                # A response may take time to arrive, so requests can be in
                # flight together (the episode test needs that).
                delay = getattr(response, "delay", 0)
                if delay:
                    await asyncio.sleep(delay)
                elif overlap:
                    await asyncio.sleep(0)
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


def _crawl(monkeypatch, script, overlap=False, storm_from=(), **kwargs):
    """Run one census over a scripted session. Returns (result-or-error, session)."""
    sessions = []

    def build(**session_kwargs):
        session = _ScriptedSession(script, overlap=overlap, storm_from=storm_from, **session_kwargs)
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


# ── Stage 4: an identifying User-Agent ─────────────────────────────────────


def test_the_tile_session_is_built_with_the_identifying_user_agent(monkeypatch):
    """Pinned at the CONSTRUCTION SITE: the session the census actually opens.

    A constant-only test would stay green if the session were built without it
    -- the shape #292's jitter default once shipped in. The session stand-in
    records the kwargs it was built with, and the real ``_fetch_tile`` then
    issues every request through that same object.
    """
    result, session = _crawl(monkeypatch, {})

    assert result["api_requests"] == len(_TILES) == len(session.urls)
    assert session.kwargs.get("headers") == {"User-Agent": dm.MAPILLARY_USER_AGENT}


def test_the_user_agent_is_the_projects_one_identity_with_the_lab_contact():
    """The same string Panoramax sends, from the one builder, so the two cannot
    drift; its RFC 9110 shape is pinned in tests/test_panoramax.py."""
    from streetscape_metadata_tracker import download_panoramax as dp
    from streetscape_metadata_tracker.download_common import STREETSCAPE_USER_AGENT_CONTACT

    ua = dm.MAPILLARY_USER_AGENT
    assert ua == dp.USER_AGENT
    assert STREETSCAPE_USER_AGENT_CONTACT == "sidewalk@cs.uw.edu"
    assert ua == (
        "StreetscapeTracker/1.0 (+https://github.com/jonfroehlich/streetscape-tracker; "
        f"sidewalk@cs.uw.edu) aiohttp/{aiohttp.__version__}"
    )
    # Never a credential: the token rides in the URL, and this string is logged.
    assert "MLY" not in ua and "token" not in ua.lower()


# ── Stage 4: backpressure on 429/503 ───────────────────────────────────────


class _RecordingLimiter:
    """A stand-in limiter that records every one built and every slowdown."""

    built: list["_RecordingLimiter"] = []

    def __init__(self, max_per_minute, *args, **kwargs):
        self.max_per_minute = max_per_minute
        self.jitter = kwargs.get("jitter")
        self.slowdowns: list[float] = []
        _RecordingLimiter.built.append(self)

    async def acquire(self):
        return None

    def slow_down(self, factor):
        self.slowdowns.append(factor)


@pytest.fixture
def limiters(monkeypatch):
    _RecordingLimiter.built = []
    monkeypatch.setattr(dm, "AsyncRateLimiter", _RecordingLimiter)
    return _RecordingLimiter.built


@pytest.fixture
def holds(monkeypatch):
    """Record every backpressure hold's sleep instead of sleeping it."""
    slept: list[float] = []

    async def record(seconds):
        slept.append(seconds)

    monkeypatch.setattr(dm, "_backpressure_sleep", record)
    return slept


@pytest.mark.parametrize("code", dm.TILE_BACKPRESSURE_STATUSES)
def test_a_429_or_503_is_raised_once_not_retried_by_backoff(code, no_backoff_sleep):
    """`@backoff` used to retry these at once, at pace, ignoring Retry-After."""
    session = _ScriptedSession({(1, 2): [status(code, **{"Retry-After": "30"})] * 10})
    counted = []
    with pytest.raises(dm.TileBackpressureError) as excinfo:
        asyncio.run(
            dm._fetch_tile(
                session,
                "https://tiles.mapillary.com/maps/vtp/mly1_computed_public/2/14/1/2?access_token=x",
                aiohttp.ClientTimeout(total=5),
                None,
                lambda: counted.append(1),
            )
        )
    assert len(session.urls) == len(counted) == 1
    assert excinfo.value.status == code
    assert excinfo.value.retry_after == "30"
    # Neither backoff's retry set nor the whole-city abort's type.
    assert not isinstance(excinfo.value, aiohttp.ClientError)
    assert not isinstance(excinfo.value, DownloadError)


def test_retry_after_reads_both_rfc_forms():
    from datetime import UTC, datetime

    now = datetime(2026, 10, 17, 9, 0, 0, tzinfo=UTC)
    assert dm.retry_after_seconds("120", now) == 120.0
    assert dm.retry_after_seconds(" 0 ", now) == 0.0
    assert dm.retry_after_seconds("Sat, 17 Oct 2026 09:05:00 GMT", now) == 300.0
    # A date already past is "now", not a negative wait.
    assert dm.retry_after_seconds("Sat, 17 Oct 2026 08:00:00 GMT", now) == 0.0
    for unreadable in (None, "", "soon", "-5", "1.5", "\u00b2", "\u0663"):
        assert dm.retry_after_seconds(unreadable, now) is None, unreadable
    # Huge but well-formed: clamped, finite, and past the ceiling -- never an
    # OverflowError (400 digits) or int()'s 4,300-digit ValueError (5,000).
    for huge in ("9" * 400, "9" * 5000, "1" + "0" * 10):
        seconds = dm.retry_after_seconds(huge, now)
        assert seconds == float(dm._RETRY_AFTER_CAP_S), huge[:12]
        assert dm.backpressure_wait_seconds(seconds) is None
    # A date too far out for datetime is unreadable, not a crash.
    assert dm.retry_after_seconds("Sat, 17 Oct 99999 09:05:00 GMT", now) is None


def test_the_wait_is_retry_after_floored_at_a_minute_and_refused_past_the_ceiling():
    floor, ceiling = dm.TILE_BACKPRESSURE_MIN_WAIT_S, dm.TILE_BACKPRESSURE_MAX_WAIT_S
    assert (floor, ceiling) == (60.0, 900.0)
    assert dm.backpressure_wait_seconds(None) == floor
    assert dm.backpressure_wait_seconds(0.0) == floor
    assert dm.backpressure_wait_seconds(59.0) == floor
    assert dm.backpressure_wait_seconds(300.0) == 300.0
    assert dm.backpressure_wait_seconds(ceiling) == ceiling
    assert dm.backpressure_wait_seconds(ceiling + 1) is None


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [
        ("120", 120.0),
        (None, 60.0),
        ("5", 60.0),
    ],
)
def test_one_429_holds_halves_and_retries_the_tile(
    monkeypatch, caplog, limiters, holds, retry_after, expected
):
    """The tile is retried after the hold, the census completes, and every
    HTTP request is counted exactly once -- the 429 included."""
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    script = {_TILES[0]: [status(429, **headers), ok()]}
    caplog.set_level(logging.INFO, logger=dm.__name__)
    result, session = _crawl(monkeypatch, script)

    assert not isinstance(result, BaseException), result
    assert result["failed_tiles"] == []
    assert result["api_requests"] == len(session.urls) == len(_TILES) + 1
    assert holds == [pytest.approx(expected, abs=1.0)]
    (limiter,) = limiters
    assert limiter.slowdowns == [dm.TILE_BACKPRESSURE_SLOWDOWN] == [2.0]
    (line,) = _status_lines(caplog)
    assert " 429=1 " in line and " retries=1 " in line


def test_an_http_date_retry_after_is_honoured_in_the_crawl(monkeypatch, limiters, holds):
    from datetime import UTC, datetime, timedelta

    when = datetime.now(UTC) + timedelta(seconds=300)
    script = {
        _TILES[0]: [status(503, **{"Retry-After": when.strftime("%a, %d %b %Y %H:%M:%S GMT")})]
    }
    result, _ = _crawl(monkeypatch, script)
    assert not isinstance(result, BaseException), result
    assert holds == [pytest.approx(300.0, abs=3.0)]


@pytest.mark.parametrize("limiter_suspends", [False, True])
def test_a_hold_stops_every_socket_not_just_the_one_answered(
    monkeypatch, limiters, limiter_suspends
):
    """With five connections, four other tiles would keep asking while the
    fifth waited. The hold is a GATE on the crawl's pacer, so none may.

    ``limiter_suspends`` makes the limiter's acquire take real time, as the
    production pacer's does, so other tiles are QUEUED ON IT -- past the gate --
    when the hold begins. Those must be sent back to wait the hold out too
    (the pacer's generation check), not released into it.
    """
    real_sleep = asyncio.sleep
    if limiter_suspends:

        async def slow_acquire(self):
            await real_sleep(0.002)

        monkeypatch.setattr(_RecordingLimiter, "acquire", slow_acquire)
    sessions_seen = []
    during = []

    async def hold(seconds):
        session = sessions_seen[0]
        before = len(session.urls)
        await real_sleep(0.05)  # let every other task run against the gate
        during.append(len(session.urls) - before)

    monkeypatch.setattr(dm, "_backpressure_sleep", hold)
    real_build = _ScriptedSession

    def build(script, **kwargs):
        session = real_build(script, **kwargs)
        sessions_seen.append(session)
        return session

    script = {_TILES[0]: [status(429), ok()]}
    monkeypatch.setattr(dm.aiohttp, "ClientSession", lambda **kw: build(script, **kw))
    result = asyncio.run(
        dm.fetch_city_images_async("Test City", _BBOX, "MLY|test|token", connection_limit=5)
    )
    assert result["api_requests"] == len(_TILES) + 1
    assert during == [0], "a tile request went out while the crawl was held"


def _three_429s(**headers):
    """connection_limit=1 makes the order exact: tiles 0 and 1 are each answered
    429 then retried; tile 2's 429 is the third, and pauses the crawl."""
    return {xy: [status(429, **headers)] for xy in _TILES[:3]}


def test_the_third_429_stops_the_crawl_as_throttled_not_a_refusal(
    monkeypatch, tmp_path, limiters, holds, caplog
):
    caplog.set_level(logging.INFO, logger=dm.__name__)
    cp = str(tmp_path / "cp")
    error, session = _crawl(monkeypatch, _three_429s(), connection_limit=1, checkpoint_path=cp)

    assert isinstance(error, HostThrottledError), error
    assert not isinstance(error, (HostBlockedError, SweepIncompleteError))
    assert host_exit_code(error) == _THROTTLED
    assert "backpressure" in str(error)
    # Tiles 0 and 1 were retried and committed; tile 2 was not.
    assert error.units_done == 2
    assert error.unit_count == len(_TILES)
    assert error.checkpoint_path == cp
    assert "next run resumes it" in str(error)
    # Two holds; the third response pauses instead of waiting.
    assert holds == [pytest.approx(60.0, abs=1.0)] * 2
    # 2 + 2 + 1 requests, each counted once, on both counters.
    assert error.api_requests == len(session.urls) == 5
    assert error.api_requests_total == 5
    (limiter,) = limiters
    assert limiter.slowdowns == [2.0], "halved ONCE per crawl, not per response"
    (line,) = _status_lines(caplog)
    assert " 429=3 " in line and " requests=5 retries=2 " in line
    # The pause warning cannot know yet which way the crawl ends, so it must
    # not claim "checkpointed" outright (review of #444): it names both.
    (warning,) = [r.getMessage() for r in caplog.records if "pausing the crawl" in r.getMessage()]
    assert "exit 87" in warning
    assert "NOT a refusal" in warning


def test_the_throttled_message_carries_the_backpressure_stop_phrase(
    monkeypatch, tmp_path, limiters, holds
):
    """The CLIs log the error's message, so the phrase is what says in the
    child's log which ceiling stopped the crawl."""
    error, _ = _crawl(
        monkeypatch, _three_429s(), connection_limit=1, checkpoint_path=str(tmp_path / "cp")
    )
    assert SWEEP_STOP_PHRASES[SWEEP_STOP_BACKPRESSURE] in str(error)


def test_a_retry_after_past_the_ceiling_pauses_at_once_without_waiting(
    monkeypatch, tmp_path, limiters, holds
):
    """Retrying before the time the server named would disobey it, so the
    crawl does not wait the ceiling and ask early: it pauses for tomorrow."""
    script = {_TILES[0]: [status(429, **{"Retry-After": "3600"})]}
    error, session = _crawl(
        monkeypatch, script, connection_limit=1, checkpoint_path=str(tmp_path / "cp")
    )
    # Nothing committed: still THROTTLED (exit 87), never the plain
    # DownloadError (exit 1) that counts a consecutive_failure -- and never a
    # claim of progress over an empty directory.
    assert type(error) is HostThrottledError
    assert host_exit_code(error) == _THROTTLED
    assert error.checkpoint_path is None and error.units_done == 0
    assert "No tile is committed" in str(error)
    assert "resumes" not in str(error)
    assert holds == []
    assert len(session.urls) == error.api_requests == 1


def test_a_retry_after_past_the_ceiling_pauses_a_crawl_with_progress(
    monkeypatch, tmp_path, limiters, holds
):
    script = {_TILES[3]: [status(429, **{"Retry-After": "3600"})]}
    error, session = _crawl(
        monkeypatch, script, connection_limit=1, checkpoint_path=str(tmp_path / "cp")
    )
    assert isinstance(error, HostThrottledError)
    assert host_exit_code(error) == _THROTTLED
    assert error.units_done == 3
    assert holds == []
    assert "Retry-After" in str(error)


@pytest.mark.parametrize("cap", [1, 2])
def test_the_request_cap_still_bounds_a_crawl_that_is_retrying(
    monkeypatch, tmp_path, limiters, holds, cap
):
    """The retry re-enters at the cap check, reservation and all.

    cap=1: the 429 IS the cap, so the retry is turned away as a request-cap
    pause and nothing is committed. cap=2: the retry is the second request.
    """
    script = {_TILES[0]: [status(429), ok()]}
    error, session = _crawl(
        monkeypatch,
        script,
        connection_limit=1,
        max_requests=cap,
        checkpoint_path=str(tmp_path / "cp"),
    )
    assert isinstance(error, DownloadError)
    assert error.api_requests == len(session.urls) == cap
    if cap == 2:
        assert isinstance(error, SweepIncompleteError)
        assert error.stopped_by == "requests"
        assert error.units_done == 1


def test_a_302_after_a_429_is_still_the_refusal(monkeypatch, limiters, holds):
    """Backpressure must never soften the block: 302 -> /login is exit 75."""
    login = {"Location": "https://www.mapillary.com/login/?next=x"}
    script = {_TILES[0]: [status(429), status(302, **login)]}
    error, session = _crawl(monkeypatch, script, connection_limit=1)
    assert isinstance(error, HostBlockedError)
    assert len(session.urls) == error.api_requests == 2


def test_halving_is_scoped_to_one_crawl(monkeypatch, limiters, holds):
    """Two crawls in one process: the second starts at the full pace."""
    first, _ = _crawl(monkeypatch, {_TILES[0]: [status(429), ok()], _TILES[1]: [status(503), ok()]})
    second, _ = _crawl(monkeypatch, {})

    assert not isinstance(first, BaseException) and not isinstance(second, BaseException)
    assert len(limiters) == 2, "each crawl builds its own limiter"
    assert limiters[0].slowdowns == [2.0]
    assert limiters[1].slowdowns == []
    assert {(lim.max_per_minute, lim.jitter) for lim in limiters} == {
        (dm.DEFAULT_TILE_REQUESTS_PER_MINUTE, dm.DEFAULT_TILE_JITTER)
    }


def test_a_backpressure_pause_resumes_for_the_missing_tiles_only(
    monkeypatch, tmp_path, limiters, holds
):
    """The point of pausing rather than failing: tomorrow pays only for what is
    left, and a full census lands. Nothing was published from the paused night."""
    cp = str(tmp_path / "cp")
    paused, _ = _crawl(monkeypatch, _three_429s(), connection_limit=1, checkpoint_path=cp)
    assert isinstance(paused, HostThrottledError)

    resumed, session = _crawl(monkeypatch, {}, checkpoint_path=cp)
    assert not isinstance(resumed, BaseException), resumed
    assert resumed["api_requests"] == len(session.urls) == len(_TILES) - 2
    assert resumed["api_requests_total"] == 5 + len(_TILES) - 2
    assert resumed["failed_tiles"] == []


def test_a_hold_that_would_outlast_the_deadline_stops_on_the_clock(
    monkeypatch, tmp_path, limiters, holds, caplog
):
    """The scheduler's SIGKILL lands 600 s after the forwarded budget, and a
    hold can be 900 s: one begun near the deadline must not sleep into the
    kill (a counted failure). Below the strike limit it is the CLOCK's stop,
    exit 83 (round-3 review of #444): it used to be 87, which skips the whole
    Mapillary host for the night on one 429 that anywhere else in the crawl
    would have been waited out. Nor does the retry go out without its wait."""
    import time as _time

    caplog.set_level(logging.INFO, logger=dm.__name__)
    script = {_TILES[3]: [status(429, **{"Retry-After": "300"})]}
    error, session = _crawl(
        monkeypatch,
        script,
        connection_limit=1,
        checkpoint_path=str(tmp_path / "cp"),
        deadline_monotonic=_time.monotonic() + 200,
    )
    assert type(error) is SweepIncompleteError, error
    assert error.stopped_by == "clock"
    assert holds == []
    assert error.units_done == 3
    # Tiles 0-2 and tile 3's 429: no retry, and no tile after it.
    assert len(session.urls) == error.api_requests == 4
    assert any("stopping on the clock now (exit 83)" in r.getMessage() for r in caplog.records)


def test_a_third_episode_near_the_deadline_is_still_throttled(
    monkeypatch, tmp_path, limiters, holds
):
    """The clock-stop rule above applies BELOW the strike limit only: the
    third standing episode says something about the host wherever in the
    crawl it lands, so it is 87 even when its wait would outlast the budget."""
    import time as _time

    # Three tiles refused with nothing served between: three episodes. The
    # deadline sits 90 s ahead (holds are recorded, not slept, so it never
    # moves): the 60 s holds of the first two fit, the third's 120 s would not.
    storm = {
        _TILES[0]: [status(503)] * 2,
        _TILES[1]: [status(503)] * 2,
        _TILES[2]: [status(503, **{"Retry-After": "120"})],
    }
    error, session = _crawl(
        monkeypatch,
        storm,
        connection_limit=1,
        checkpoint_path=str(tmp_path / "cp"),
        deadline_monotonic=_time.monotonic() + 90,
    )
    assert type(error) is HostThrottledError, error
    assert host_exit_code(error) == _THROTTLED
    assert "3 HTTP 429/503 episodes" in str(error)


# ── Backpressure after the crawl already stopped (round-2 review of #444) ─


def _slow(code, delay, **headers):
    r = status(code, **headers)
    r.delay = delay
    return r


def test_a_429_after_the_cap_is_spent_neither_holds_nor_throttles(
    monkeypatch, tmp_path, limiters, holds
):
    """Tile 0's 429 lands after tile 1 spent the cap: no admitted tile is left
    to send, so a hold would only sleep holding the machine-wide tile lock.
    It stays the cap's exit 83. (Under round 7's reservations, held to the
    ANSWER, tile 1 was turned away while tile 0's request was still counted
    twice, and tile 0's retry then went out at once, unheld -- round 8.)"""
    script = {_TILES[0]: [_slow(429, 0.05, **{"Retry-After": "5"}), ok()]}
    error, session = _crawl(
        monkeypatch,
        script,
        connection_limit=2,
        max_requests=2,
        checkpoint_path=str(tmp_path / "cp"),
    )
    assert type(error) is SweepIncompleteError, error
    assert error.stopped_by == "requests"
    assert holds == []
    asked = [tuple(int(g) for g in _URL_TILE.search(u).groups()) for u in session.urls]
    assert asked == [_TILES[0], _TILES[1]]
    assert error.units_done == 1
    assert len(session.urls) == error.api_requests == 2


def test_a_429_at_the_cap_with_every_reserved_request_on_the_wire_does_not_hold(
    monkeypatch, tmp_path, limiters, holds
):
    """Round-8 review: a reservation is released only when its ANSWER comes
    back, so `reserved` also counts requests already sent. At the cap with
    every admitted request already on the wire nothing more can go out, and
    a hold would only sleep -- up to 899 s holding the tile lock. Round 7
    gated the hold on `reserved` and slept it; the gate is now on requests
    admitted but not yet sent."""
    script = {_TILES[0]: [_slow(429, 0.1, **{"Retry-After": "899"}), ok()]}
    for t in _TILES[1:5]:
        script[t] = [_slow(200, 0.3, **{"Content-Type": "application/x-protobuf"})]
    error, session = _crawl(
        monkeypatch,
        script,
        connection_limit=5,
        max_requests=5,
        checkpoint_path=str(tmp_path / "cp"),
    )
    assert type(error) is SweepIncompleteError, error
    assert error.stopped_by == "requests"
    assert holds == []
    assert len(session.urls) == error.api_requests


def test_a_429_after_a_clock_stop_is_still_the_clocks_83_not_87(
    monkeypatch, tmp_path, limiters, holds
):
    """After a clock stop every wait "outlasts the deadline", so this used to
    become exit 87 on any first 429. No wait is going to happen, so it no
    longer escalates: the stop is the clock's."""
    script = {
        _TILES[0]: [_slow(429, 0.3)],
        _TILES[1]: [_slow(200, 0.1, **{"Content-Type": "application/x-protobuf"})],
    }
    error, session = _crawl(
        monkeypatch,
        script,
        connection_limit=2,
        checkpoint_path=str(tmp_path / "cp"),
        deadline_monotonic=_clock_runs_out_after(monkeypatch, 2),
    )
    assert type(error) is SweepIncompleteError, error
    assert error.stopped_by == "clock"
    assert holds == []
    assert len(session.urls) == 2


@pytest.fixture
def quick_backoff(monkeypatch):
    """``@backoff``'s retry sleep, cut to a few event-loop turns.

    Patched on backoff's own module rather than ``asyncio`` globally, so the
    responses' scripted delays (real sleeps) still order the requests.
    """
    import backoff._async as backoff_async

    real_sleep = asyncio.sleep

    async def quick(delay, *args, **kwargs):
        await real_sleep(0.02)

    monkeypatch.setattr(backoff_async.asyncio, "sleep", quick)


def _clock_runs_out_after(monkeypatch, n_requests):
    """A wall-clock deadline that passes once ``n_requests`` have gone out.

    The clock tests need every tile ADMITTED before the deadline and the
    deadline PASSED by the time a slow response lands. A real deadline a few
    milliseconds away encodes that as a race against the runner: on a loaded
    CI box (py3.14, twice on 2026-10-21) it passed before the first tile was
    admitted, nothing went out, and the test failed on `_asked() == []`.
    Counting requests makes the order the test means the order it gets.

    Returns a far-future ``deadline_monotonic``, so the real clock never stops
    the crawl and the "a wait would outlast the deadline" arm is decided by
    ``crawl_deadline_passed`` alone, as it is once a real deadline has passed.
    """
    sent = []
    real_get = _ScriptedSession.get

    def counting_get(self, url, **kwargs):
        sent.append(url)
        return real_get(self, url, **kwargs)

    monkeypatch.setattr(_ScriptedSession, "get", counting_get)
    monkeypatch.setattr(dm, "crawl_deadline_passed", lambda deadline: len(sent) >= n_requests)
    return time.monotonic() + 3600


def _asked(session):
    return [tuple(int(g) for g in _URL_TILE.search(u).groups()) for u in session.urls]


def test_a_backoff_retry_after_a_429_at_the_cap_waits_out_the_hold(
    monkeypatch, tmp_path, limiters, holds, quick_backoff
):
    """Round-9 review: tile 0 is on the wire when tile 1's 429 lands at a
    spent cap. Nothing admitted is left to send, so the hold was skipped --
    but tile 0's 500 is then retried by `@backoff`, which holds no
    reservation, and that retry went out unheld. The hold is now ARMED: the
    retry sleeps it before it goes out."""
    script = {
        _TILES[0]: [_slow(500, 0.01), ok()],
        _TILES[1]: [_slow(429, 0.005, **{"Retry-After": "60"}), ok()],
    }
    error, session = _crawl(
        monkeypatch,
        script,
        connection_limit=2,
        max_requests=2,
        checkpoint_path=str(tmp_path / "cp"),
    )
    assert type(error) is SweepIncompleteError, error
    assert error.stopped_by == "requests"
    # The retry did go out (it holds no reservation; the residue the cap
    # tolerates), but only after the 429's hold.
    assert _asked(session) == [_TILES[0], _TILES[1], _TILES[0]]
    assert len(holds) == 1 and holds[0] == pytest.approx(60, abs=1)


def test_a_backoff_retry_after_a_429_past_the_clock_is_turned_away(
    monkeypatch, tmp_path, limiters, holds, quick_backoff
):
    """Round-9 review, clock variant: past the deadline no retry may wait, so
    the pacer closes instead of arming, and tile 0's `@backoff` retry is
    turned away rather than sent unheld. (No tile is committed in a two-tile
    script, so this is the no-checkpoint `DownloadError`, not exit 83.)"""
    script = {
        _TILES[0]: [_slow(500, 0.01), ok()],
        _TILES[1]: [_slow(429, 0.005, **{"Retry-After": "60"}), ok()],
    }
    error, session = _crawl(
        monkeypatch,
        script,
        connection_limit=2,
        checkpoint_path=str(tmp_path / "cp"),
        deadline_monotonic=_clock_runs_out_after(monkeypatch, 2),
    )
    assert isinstance(error, DownloadError), error
    assert holds == []
    assert _asked(session) == [_TILES[0], _TILES[1]]


def test_a_capped_429_whose_hold_outlasts_the_deadline_closes_and_names_the_cap(
    monkeypatch, tmp_path, limiters, holds, quick_backoff
):
    """Round-10 review: the cap is spent and the 60 s hold would outlast a
    deadline 30 s away (not yet passed). Arming it would have the `@backoff`
    retry sleep into the SIGKILL margin, so the pacer closes and the retry is
    turned away -- and the stop is named for the cap that caused it, not the
    clock. (No tile is committed in a two-tile script, so this is the
    no-checkpoint `DownloadError`.)"""
    import time as _time

    script = {
        _TILES[0]: [_slow(500, 0.01), ok()],
        _TILES[1]: [_slow(429, 0.005, **{"Retry-After": "60"}), ok()],
    }
    error, session = _crawl(
        monkeypatch,
        script,
        connection_limit=2,
        max_requests=2,
        checkpoint_path=str(tmp_path / "cp"),
        deadline_monotonic=_time.monotonic() + 30,
    )
    assert isinstance(error, DownloadError), error
    assert "wall-clock" not in str(error), error
    assert holds == []
    assert _asked(session) == [_TILES[0], _TILES[1]]


def test_a_shorter_armed_hold_does_not_cut_a_longer_one(
    monkeypatch, tmp_path, limiters, holds, quick_backoff
):
    """Round-10 review: two 429s at a spent cap, Retry-After 300 then 10,
    while a 500's `@backoff` retry is in flight. The armed hold keeps the
    LATER end, as overlapping holds do, so the retry waits the full 300 s."""
    script = {
        _TILES[0]: [_slow(500, 0.02), ok()],
        _TILES[1]: [_slow(429, 0.005, **{"Retry-After": "300"}), ok()],
        _TILES[2]: [_slow(429, 0.01, **{"Retry-After": "10"}), ok()],
    }
    error, session = _crawl(
        monkeypatch,
        script,
        connection_limit=3,
        max_requests=3,
        checkpoint_path=str(tmp_path / "cp"),
    )
    assert type(error) is SweepIncompleteError, error
    assert len(holds) == 1 and holds[0] == pytest.approx(300, abs=1), holds


def test_a_stuck_last_tile_does_not_sleep_the_hold_it_gives_up_on(
    monkeypatch, tmp_path, limiters, holds
):
    """Round-9 review: the LAST tile, refused on its final try, slept a full
    899 s Retry-After holding the tile lock with nothing left to send. Its
    first refusal still holds (its own retry follows); the give-up's hold is
    armed, and nothing goes out after it, so nobody sleeps it."""
    script = {_TILES[-1]: [status(429, **{"Retry-After": "899"})] * 5}
    result, session = _crawl(
        monkeypatch, script, connection_limit=1, checkpoint_path=str(tmp_path / "cp")
    )
    assert result["failed_tiles"] == [_TILES[-1]]
    assert len(holds) == 1 and holds[0] == pytest.approx(899, abs=1)
    assert len(session.urls) == len(_TILES) - 1 + dm.TILE_BACKPRESSURE_TILE_TRIES


def test_a_long_retry_after_after_the_cap_tripped_is_still_throttled(
    monkeypatch, tmp_path, limiters, holds
):
    """A Retry-After past the ceiling says something about the HOST, so it
    still escalates to exit 87 after a cap stop -- which also pins that the
    throttle is chosen on `backpressure_paused`, not on `stop_reason` (the cap
    set that first, to "requests")."""
    script = {_TILES[0]: [_slow(429, 0.05, **{"Retry-After": "3600"})]}
    error, session = _crawl(
        monkeypatch,
        script,
        connection_limit=2,
        max_requests=2,
        checkpoint_path=str(tmp_path / "cp"),
    )
    assert type(error) is HostThrottledError, error
    assert host_exit_code(error) == _THROTTLED
    assert "Retry-After" in str(error)
    assert holds == []


def test_a_shorter_hold_never_cuts_short_a_longer_one(monkeypatch):
    """Overlapping holds end at the LATEST requested end.

    Three 429s land together: A asks 10 s, B 100 s, C 5 s. A takes the gate
    and sleeps 10; B, queued behind it, must then sleep the 90 s still owed to
    its own 100 s. If C's 5 s could overwrite the end B registered, B would
    find nothing left and the crawl would resume 90 s early.
    """
    clock = {"t": 0.0}
    slept = []

    real_sleep = asyncio.sleep

    async def sleep(seconds):
        slept.append(seconds)
        await real_sleep(0)  # yield, so the other holds queue behind this one
        clock["t"] += seconds

    monkeypatch.setattr(dm, "_backpressure_sleep", sleep)
    pacer = dm._BackpressurePacer(_RecordingLimiter(40), clock=lambda: clock["t"])

    async def scenario():
        await asyncio.gather(pacer.hold(10), pacer.hold(100), pacer.hold(5))

    asyncio.run(scenario())
    assert slept == [pytest.approx(10.0), pytest.approx(90.0)]
    assert clock["t"] == pytest.approx(100.0)


def test_no_request_goes_out_after_the_pause_decision(monkeypatch, tmp_path, limiters):
    """Review of #444: with five sockets and a limiter that takes real time,
    requests already waiting on the pacer used to be released AFTER the third
    429 decided to pause -- four of seven in the reproduction. A closed pacer
    turns them away uncommitted instead."""
    real_sleep = asyncio.sleep

    async def slow_acquire(self):
        await real_sleep(0.002)

    async def hold(seconds):
        await real_sleep(0.01)

    monkeypatch.setattr(_RecordingLimiter, "acquire", slow_acquire)
    monkeypatch.setattr(dm, "_backpressure_sleep", hold)

    sessions = []
    script = {xy: [status(429)] for xy in _TILES[:3]}

    def build(**kwargs):
        sessions.append(_ScriptedSession(script, **kwargs))
        return sessions[-1]

    sent_at_pause = []

    class _AtPause(logging.Handler):
        def emit(self, record):
            if "pausing the crawl" in record.getMessage():
                sent_at_pause.append(len(sessions[0].urls))

    handler = _AtPause()
    logging.getLogger(dm.__name__).addHandler(handler)
    monkeypatch.setattr(dm.aiohttp, "ClientSession", build)
    try:
        # THROTTLED either way, whether or not a tile committed first.
        with pytest.raises(HostThrottledError) as excinfo:
            asyncio.run(
                dm.fetch_city_images_async(
                    "Test City",
                    _BBOX,
                    "MLY|test|token",
                    connection_limit=5,
                    checkpoint_path=str(tmp_path / "cp"),
                )
            )
    finally:
        logging.getLogger(dm.__name__).removeHandler(handler)

    assert len(sent_at_pause) == 1, "the crawl must decide to pause exactly once"
    assert len(sessions[0].urls) == sent_at_pause[0], "a request went out after the pause"
    assert excinfo.value.api_requests == len(sessions[0].urls)
    assert SWEEP_STOP_PHRASES[SWEEP_STOP_BACKPRESSURE] in str(excinfo.value)


def test_a_tile_that_always_answers_503_cannot_stall_its_city(
    monkeypatch, tmp_path, limiters, holds
):
    """Review of #444: one tile answering 503 to everything spent all three
    strikes, the crawl paused (exit 83, amnestied without bound), and the next
    night hit the same tile first -- forever. A tile is now given up as an
    ordinary failed tile after TILE_BACKPRESSURE_TILE_TRIES responses, below
    the pause, so the city completes and #168's tolerance judges the hole.

    Same checkpoint across nights, as the scheduler would run it: the city
    must complete within a bounded number of nights (one, in fact).
    """
    assert dm.TILE_BACKPRESSURE_TILE_TRIES < dm.TILE_BACKPRESSURE_PAUSE_AFTER
    cp = str(tmp_path / "cp")
    bad = _TILES[100]
    outcomes = []
    for _night in range(3):
        outcome, session = _crawl(
            monkeypatch, {bad: [status(503)] * 50}, connection_limit=1, checkpoint_path=cp
        )
        outcomes.append(outcome)
        if not isinstance(outcome, BaseException):
            break
    assert len(outcomes) == 1, [type(o).__name__ for o in outcomes]
    result = outcomes[0]
    assert result["failed_tiles"] == [bad]
    # The bad tile drew exactly its allowance, each request counted once.
    bad_urls = [u for u in session.urls if f"/{bad[0]}/{bad[1]}?" in u]
    assert len(bad_urls) == dm.TILE_BACKPRESSURE_TILE_TRIES
    assert result["api_requests"] == len(session.urls) == len(_TILES) - 1 + len(bad_urls)


@pytest.mark.parametrize("n_stuck", [2, 3])
def test_several_tiles_that_always_answer_503_cannot_stall_their_city(
    monkeypatch, tmp_path, limiters, holds, n_stuck
):
    """Round-2 review of #444: two stuck tiles spent three strikes between them
    (A's two responses, B's first), the crawl paused at B as exit 87, and since
    a failed tile is never committed every resumed night did the same -- and
    87 latches the host, so one city blocked all Mapillary every night.

    A tile's own retry is no strike, and a stuck tile's first-response strike
    is withdrawn once another tile is served, so the city completes on its
    first night, the stuck tiles failed tiles under #168's tolerance.
    """
    cp = str(tmp_path / "cp")
    bad = [_TILES[50], _TILES[150], _TILES[250]][:n_stuck]
    outcomes = []
    for _night in range(3):
        outcome, session = _crawl(
            monkeypatch,
            {b: [status(503)] * 50 for b in bad},
            connection_limit=1,
            checkpoint_path=cp,
        )
        outcomes.append(outcome)
        if not isinstance(outcome, BaseException):
            break
    assert len(outcomes) == 1, [type(o).__name__ for o in outcomes]
    result = outcomes[0]
    assert sorted(result["failed_tiles"]) == sorted(bad)
    for b in bad:
        urls = [u for u in session.urls if f"/{b[0]}/{b[1]}?" in u]
        assert len(urls) == dm.TILE_BACKPRESSURE_TILE_TRIES


def test_a_tiles_own_retry_is_no_strike(monkeypatch, tmp_path, limiters, holds):
    """Two stuck tiles side by side, no tile served between them: no strike is
    withdrawn before tile 2's first 429, so only the first-response rule keeps
    the crawl from pausing there (it is the second episode, not the third)."""
    stuck = {xy: [status(503)] * 5 for xy in _TILES[1:3]}
    result, session = _crawl(
        monkeypatch, stuck, connection_limit=1, checkpoint_path=str(tmp_path / "cp")
    )
    assert not isinstance(result, BaseException), result
    assert sorted(result["failed_tiles"]) == sorted(_TILES[1:3])


def test_a_host_wide_503_storm_still_pauses_at_the_third_episode(
    monkeypatch, tmp_path, limiters, holds
):
    """Every tile answers 503 to everything: nothing is served between the
    stuck tiles, so no strike is withdrawn -- tiles 0, 1 and 2 are three
    episodes and the crawl pauses as THROTTLED, as before the round-2 fix."""
    storm = {xy: [status(503)] * 5 for xy in _TILES}
    error, session = _crawl(
        monkeypatch, storm, connection_limit=1, checkpoint_path=str(tmp_path / "cp")
    )
    assert type(error) is HostThrottledError, error
    assert host_exit_code(error) == _THROTTLED
    assert "3 HTTP 429/503 episodes" in str(error)
    assert error.units_done == 0
    # Tiles 0 and 1 drew both their responses, tile 2 its first: 5 requests.
    assert len(session.urls) == error.api_requests == 5


def _nights_until_complete(monkeypatch, script, checkpoint_path, connection_limit, nights=3):
    """Run the census over one checkpoint, night after night, as the scheduler
    would, until it completes or ``nights`` run out. Returns (outcomes, last session)."""
    outcomes = []
    for _night in range(nights):
        outcome, session = _crawl(
            monkeypatch,
            script(),
            connection_limit=connection_limit,
            checkpoint_path=checkpoint_path,
        )
        outcomes.append(outcome)
        if not isinstance(outcome, BaseException):
            break
    return outcomes, session


@pytest.mark.parametrize("connection_limit", [1, 5])
@pytest.mark.parametrize(
    "stuck",
    [(0, 2, 4), (10, 11, 13), (10, 12, 14), (100, 103, 106)],
    ids=["start-two-apart", "pair-then-one", "two-apart", "three-apart"],
)
def test_closely_spaced_stuck_tiles_complete_their_city(
    monkeypatch, tmp_path, limiters, holds, connection_limit, stuck
):
    """Round-3 review of #444: round 2 withdrew a stuck tile's strike only on a
    success AFTER its give-up, which takes two holds -- and the hold's FIFO gate
    sends the next stuck tile's first request before any success lands. Three
    stuck tiles two apart at five sockets (prod's) paused the city as 87 every
    night with no progress, and 87 skips the host for the night.

    A strike now stops counting as soon as another tile is served from a later
    pacer generation, so any stuck tiles with a served tile between them in
    request order complete the city on its FIRST night, as failed tiles.
    """
    bad = [_TILES[i] for i in stuck]
    outcomes, session = _nights_until_complete(
        monkeypatch,
        lambda: {b: [status(503)] * 50 for b in bad},
        str(tmp_path / "cp"),
        connection_limit,
    )
    assert len(outcomes) == 1, [type(o).__name__ for o in outcomes]
    result = outcomes[0]
    assert not isinstance(result, BaseException), result
    assert sorted(result["failed_tiles"]) == sorted(bad)
    for b in bad:
        urls = [u for u in session.urls if f"/{b[0]}/{b[1]}?" in u]
        assert len(urls) == dm.TILE_BACKPRESSURE_TILE_TRIES
    assert result["api_requests"] == len(session.urls)
    assert len(session.urls) == len(_TILES) - len(bad) + len(bad) * dm.TILE_BACKPRESSURE_TILE_TRIES


@pytest.mark.parametrize("connection_limit", [1, 5])
def test_three_stuck_tiles_in_a_row_cannot_be_told_from_a_storm(
    monkeypatch, tmp_path, limiters, holds, connection_limit
):
    """The rule's documented limit, pinned so that changing it is a decision:
    three stuck tiles NEXT to each other in request order are refused with
    nothing served between them, which is exactly what a host-wide storm looks
    like from inside one crawl, so the third is a third episode and the crawl
    stops as 87. (Holds release requests in FIFO order, so five sockets do not
    interleave a served tile between them.) Nothing a single night observes
    tells the two apart -- but the next night does: the tiles that night saw
    refused and never served are in the checkpoint, so the resume completes
    (round 4). See
    docs/provider-access.md, rule 6."""
    bad = [_TILES[i] for i in (10, 11, 12)]
    cp = str(tmp_path / "cp")
    error, session = _crawl(
        monkeypatch,
        {b: [status(503)] * 50 for b in bad},
        connection_limit=connection_limit,
        checkpoint_path=cp,
    )
    assert type(error) is HostThrottledError, error
    assert "3 HTTP 429/503 episodes" in str(error)
    assert error.units_done == 10
    # ...for ONE night. The three refused, unserved tiles are in the checkpoint,
    # so the resume retries them last and they charge no strike: night 2
    # completes. (At five sockets none was even given up before the pause.)
    result, session = _crawl(
        monkeypatch,
        {b: [status(503)] * 50 for b in bad},
        connection_limit=connection_limit,
        checkpoint_path=cp,
    )
    assert not isinstance(result, BaseException), result
    assert sorted(result["failed_tiles"]) == sorted(bad)


@pytest.mark.parametrize("connection_limit", [1, 5])
def test_a_resume_does_not_line_up_last_nights_stuck_tiles_as_a_storm(
    monkeypatch, tmp_path, limiters, holds, connection_limit
):
    """Round-4 review of #444: a failed tile is never committed, so a resumed
    crawl's `todo` loses the served tiles that sat BETWEEN last night's stuck
    tiles. Three stuck tiles anywhere in a capped night's prefix then met at
    the head of every later night, nothing served between them -- a storm as
    one crawl sees it -- and each night ended as 87 with no tile committed,
    latching the host, until the checkpoint aged out and the cycle restarted.

    The given-up tiles are recorded in the checkpoint; a resume retries them
    LAST, and none of them charges a strike, so night 2 completes the city."""
    bad = [_TILES[i] for i in (2, 8, 14)]
    cp = str(tmp_path / "cp")

    def script():
        return {b: [status(503)] * 50 for b in bad}

    first, _session = _crawl(
        monkeypatch,
        script(),
        connection_limit=connection_limit,
        checkpoint_path=cp,
        max_requests=30,
    )
    assert type(first) is SweepIncompleteError, first
    assert 0 < first.units_done < len(_TILES)
    with open(dm._state_path(cp), encoding="utf-8") as f:
        recorded = {tuple(t) for t in json.load(f)["stuck_tiles"]}
    assert recorded == set(bad)

    outcomes, session = _nights_until_complete(monkeypatch, script, cp, connection_limit)
    assert len(outcomes) == 1, [str(o)[:120] for o in outcomes]
    result = outcomes[0]
    assert not isinstance(result, BaseException), result
    assert sorted(result["failed_tiles"]) == sorted(bad)
    # Retried last: every fresh tile was asked for before any stuck one.
    order = [tuple(int(g) for g in _URL_TILE.search(u).groups()) for u in session.urls]
    first_stuck = min(order.index(b) for b in bad)
    assert set(order[:first_stuck]).isdisjoint(bad)
    assert len(set(order[:first_stuck])) == len(_TILES) - first.units_done - len(bad)


def test_a_storm_still_pauses_a_resume_on_its_fresh_tiles(monkeypatch, tmp_path, limiters, holds):
    """The other side of the round-4 rule: forgiving last night's stuck tiles
    must not blind a resume to a real storm. Fresh tiles go first and still
    charge strikes, so a storm on every tile pauses at their third episode,
    before any previously stuck tile is asked for."""
    bad = [_TILES[i] for i in (2, 8, 14)]
    cp = str(tmp_path / "cp")
    first, _ = _crawl(
        monkeypatch,
        {b: [status(503)] * 50 for b in bad},
        connection_limit=1,
        checkpoint_path=cp,
        max_requests=30,
    )
    assert type(first) is SweepIncompleteError, first
    error, session = _crawl(
        monkeypatch,
        {xy: [status(503)] * 5 for xy in _TILES},
        connection_limit=1,
        checkpoint_path=cp,
    )
    assert type(error) is HostThrottledError, error
    assert "3 HTTP 429/503 episodes" in str(error)
    asked = {tuple(int(g) for g in _URL_TILE.search(u).groups()) for u in session.urls}
    assert asked.isdisjoint(bad)


def _seed_resume(cp, n_fresh, recorded):
    """A checkpoint holding every tile but ``n_fresh`` fresh ones and the
    ``recorded`` stuck ones, all committed empty (an empty tile has no part)."""
    os.makedirs(cp, exist_ok=True)
    rest = [t for t in _TILES if t not in recorded]
    done = dict.fromkeys(rest[n_fresh:], 0)
    dm._write_checkpoint_state(
        dm.TileCheckpoint(path=cp, stuck=set(recorded)),
        done,
        bbox=_BBOX,
        tiles=_TILES,
        api_requests_total=len(done),
    )
    return rest[:n_fresh]


@pytest.mark.parametrize("overlap", [False, True], ids=["serial", "overlap"])
@pytest.mark.parametrize("connection_limit", [1, 5])
@pytest.mark.parametrize("n_fresh", [0, 2])
def test_a_storm_pauses_a_resume_whose_fresh_tiles_cannot(
    monkeypatch, tmp_path, limiters, holds, connection_limit, n_fresh, overlap
):
    """Round-5 review of #444: recorded tiles charge no strike, so with fewer
    than three fresh tiles left a host-wide storm could not pause the resume --
    it finalized with the storm's tiles as holes, or failed as a counted
    failure after holding the host lock. A recorded tile refused with nothing
    served since now re-requests a COMMITTED tile, whose refusal is a strike."""
    cp = str(tmp_path / "cp")
    recorded = [_TILES[i] for i in (5, 6, 7)]
    _seed_resume(cp, n_fresh, recorded)
    error, session = _crawl(
        monkeypatch,
        {xy: [status(503)] * 10 for xy in _TILES},
        overlap=overlap,
        connection_limit=connection_limit,
        checkpoint_path=cp,
    )
    assert type(error) is HostThrottledError, error
    assert host_exit_code(error) == _THROTTLED
    assert "3 HTTP 429/503 episodes" in str(error)
    assert error.api_requests == len(session.urls)


@pytest.mark.parametrize("overlap", [False, True], ids=["serial", "overlap"])
@pytest.mark.parametrize("connection_limit", [1, 5])
@pytest.mark.parametrize("n_recorded", [1, 3, 6])
@pytest.mark.parametrize("n_fresh", [1, 4])
def test_a_storm_that_begins_at_the_recorded_tail_pauses(
    monkeypatch, tmp_path, limiters, holds, connection_limit, n_fresh, n_recorded, overlap
):
    """Round-6 review: the fresh tiles are SERVED and the storm begins with the
    first recorded tile. With the probe gated on `served_generation <
    sent_generation` the first recorded tile -- sent in the generation a fresh
    tile was served in -- never probed, so three recorded tiles reached two
    strikes and the night finalized with them as holes. And with requests in
    flight together, one probe per recorded tile went out as ONE episode. The
    probe now keeps asking until served or paused."""
    cp = str(tmp_path / "cp")
    recorded = _TILES[5 : 5 + n_recorded]
    _seed_resume(cp, n_fresh, recorded)
    error, session = _crawl(
        monkeypatch,
        {},
        overlap=overlap,
        storm_from=recorded,
        connection_limit=connection_limit,
        checkpoint_path=cp,
    )
    assert type(error) is HostThrottledError, error
    assert "3 HTTP 429/503 episodes" in str(error)
    assert error.api_requests == len(session.urls)


@pytest.mark.parametrize("overlap", [False, True], ids=["serial", "overlap"])
@pytest.mark.parametrize("connection_limit", [1, 5])
def test_recorded_tiles_alone_complete_when_the_host_serves(
    monkeypatch, tmp_path, limiters, holds, connection_limit, overlap
):
    """The probe's other answer: the host serves the committed tile, so the
    recorded tiles are the ones refusing and the resume completes, each
    drawing its two requests and at most one probe per refusal."""
    cp = str(tmp_path / "cp")
    recorded = [_TILES[i] for i in (5, 6, 7)]
    _seed_resume(cp, 0, recorded)
    result, session = _crawl(
        monkeypatch,
        {b: [status(503)] * 10 for b in recorded},
        overlap=overlap,
        connection_limit=connection_limit,
        checkpoint_path=cp,
    )
    assert not isinstance(result, BaseException), result
    assert sorted(result["failed_tiles"]) == sorted(recorded)
    asked = [tuple(int(g) for g in _URL_TILE.search(u).groups()) for u in session.urls]
    for b in recorded:
        assert asked.count(b) == dm.TILE_BACKPRESSURE_TILE_TRIES
    # At most one probe per refusal: each is served, so none loops.
    probes = [t for t in asked if t not in recorded]
    assert 1 <= len(probes) <= len(recorded) * dm.TILE_BACKPRESSURE_TILE_TRIES
    assert result["api_requests"] == len(session.urls)


def test_a_served_probe_withdraws_the_strikes_before_it(monkeypatch, tmp_path, limiters, holds):
    """A probe served is the host serving: it withdraws pending strikes like
    any served tile. Two stuck fresh tiles leave two pending strikes; the
    first probe is served, so the second probe's lone 429 is episode 1, not
    a third -- without the withdrawal this resume stopped as 87."""
    cp = str(tmp_path / "cp")
    recorded = [_TILES[i] for i in (5, 6, 7)]
    fresh = _seed_resume(cp, 2, recorded)
    pool = [t for t in _TILES if t not in recorded][2:]
    script = {t: [status(503)] * 10 for t in fresh + recorded}
    script[pool[1]] = [status(429)]
    result, session = _crawl(monkeypatch, script, connection_limit=1, checkpoint_path=cp)
    assert not isinstance(result, BaseException), result
    assert sorted(result["failed_tiles"]) == sorted(fresh + recorded)
    asked = [tuple(int(g) for g in _URL_TILE.search(u).groups()) for u in session.urls]
    assert pool[1] in asked


def test_a_refused_probe_is_held_before_anything_else_goes_out(
    monkeypatch, tmp_path, limiters, holds
):
    """Rule 2 holds for a probe too: a probe's 429 holds every request before
    the next one -- the next probe, and the recorded tile's own retry."""
    cp = str(tmp_path / "cp")
    recorded = [_TILES[5]]
    _seed_resume(cp, 0, recorded)
    pool = [t for t in _TILES if t not in recorded]
    script = {recorded[0]: [status(503)] * 10, pool[0]: [status(429)]}
    result, session = _crawl(monkeypatch, script, connection_limit=1, checkpoint_path=cp)
    assert not isinstance(result, BaseException), result
    asked = [tuple(int(g) for g in _URL_TILE.search(u).groups()) for u in session.urls]
    # R refused (hold), probe 1 refused (hold), probe 2 served, R's retry
    # refused (hold, given up), probe 3 served.
    assert asked == [recorded[0], pool[0], pool[1], recorded[0], pool[2]]
    assert len(holds) == 3


def test_no_probe_goes_out_once_the_clock_has_run_out(monkeypatch, tmp_path, limiters, holds):
    """The probe is gated on the deadline like a fresh tile: a clock that runs
    out during the recorded tile's hold sends no probe, and the crawl stops
    on the clock (exit 83)."""
    import time as _time

    cp = str(tmp_path / "cp")
    recorded = [_TILES[5]]
    _seed_resume(cp, 0, recorded)
    expired = []

    async def hold_then_expire(seconds):
        holds.append(seconds)
        expired.append(True)

    monkeypatch.setattr(dm, "_backpressure_sleep", hold_then_expire)
    monkeypatch.setattr(dm, "crawl_deadline_passed", lambda deadline: bool(expired))
    error, session = _crawl(
        monkeypatch,
        {recorded[0]: [status(503)] * 10},
        connection_limit=1,
        checkpoint_path=cp,
        deadline_monotonic=_time.monotonic() + 10_000,
    )
    assert type(error) is SweepIncompleteError, error
    assert error.stopped_by == "clock"
    assert len(session.urls) == 1


def test_a_302_to_a_probe_is_the_refusal_at_once(monkeypatch, tmp_path, limiters, holds):
    """A probe redirected to /login answers for the whole city: exit 75, and
    the recorded tile's own retry never goes out."""
    cp = str(tmp_path / "cp")
    recorded = [_TILES[5]]
    _seed_resume(cp, 0, recorded)
    pool = [t for t in _TILES if t not in recorded]
    login = {"Location": "https://www.mapillary.com/login/?next=x%3Faccess_token%3DMLY"}
    script = {recorded[0]: [status(503)] * 10, pool[0]: [status(302, **login)]}
    error, session = _crawl(monkeypatch, script, connection_limit=1, checkpoint_path=cp)
    assert type(error) is HostBlockedError, error
    asked = [tuple(int(g) for g in _URL_TILE.search(u).groups()) for u in session.urls]
    assert asked == [recorded[0], pool[0]]


@pytest.mark.parametrize("overlap", [False, True], ids=["serial", "overlap"])
@pytest.mark.parametrize("connection_limit", [1, 5])
def test_a_pause_decided_by_the_probe_on_a_tiles_last_try_is_87(
    monkeypatch, tmp_path, limiters, holds, connection_limit, overlap
):
    """Round-7 review: the recorded tile's retry is refused, and the probe it
    sends runs into a storm and pauses the crawl. The tile then took the
    give-up branch -- a failed tile, not a turned-away one -- so nothing set
    `capped`, and with no other tile left the census FINALIZED (exit 0) under
    a pause already logged as exit 87, the host unlatched."""
    cp = str(tmp_path / "cp")
    recorded = [_TILES[5]]
    _seed_resume(cp, 0, recorded)
    pool = [t for t in _TILES if t not in recorded]
    error, session = _crawl(
        monkeypatch,
        {recorded[0]: [status(503)] * 10},
        overlap=overlap,
        storm_from=[pool[1]],
        connection_limit=connection_limit,
        checkpoint_path=cp,
    )
    assert type(error) is HostThrottledError, error
    assert "3 HTTP 429/503 episodes" in str(error)


def test_a_probe_refused_one_request_short_of_the_cap_still_holds(
    monkeypatch, tmp_path, limiters, holds
):
    """A probe answered 429 one request short of the cap is not a stopping
    crawl's 429: it holds, and the next probe spends the last request."""
    cp = str(tmp_path / "cp")
    recorded = [_TILES[5]]
    _seed_resume(cp, 0, recorded)
    pool = [t for t in _TILES if t not in recorded]
    script = {recorded[0]: [status(503)] * 10, pool[0]: [status(429)]}
    error, session = _crawl(
        monkeypatch, script, connection_limit=1, checkpoint_path=cp, max_requests=3
    )
    assert type(error) is SweepIncompleteError, error
    assert error.stopped_by == "requests"
    # R refused (hold), probe 1 refused at request 2 of 3 (hold), probe 2
    # served at the cap; R's retry is turned away.
    assert len(session.urls) == 3
    assert len(holds) == 2


class _JitteredSession(_ScriptedSession):
    """Answers after a random number of yields, logging sends, answers and
    holds in one event list, so request order and hold discipline can be
    read back across overlapping sockets."""

    def __init__(self, script, rng, events, **kwargs):
        super().__init__(script, **kwargs)
        self._rng = rng
        self._events = events

    def get(self, url, **kwargs):
        m = _URL_TILE.search(url)
        tile = (int(m.group(1)), int(m.group(2)))
        self._events.append(("send", tile))
        inner = super().get(url, **kwargs)
        yields = self._rng.choice([0, 1, 2, 3, 5, 8])
        events = self._events

        class _Ctx:
            async def __aenter__(self):
                for _ in range(yields):
                    await asyncio.sleep(0)
                response = await inner.__aenter__()
                events.append(("answer", response.status, tile))
                return response

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def _sent_after_unheld_refusal(events) -> bool:
    """True if any request went out after a 429/503 with no hold between.

    A refusal answering a request sent BEFORE some hold began is already
    covered by that hold -- the same episode, rule 6 -- so it owes nothing.
    """
    owed = False
    last_hold = -1
    sent_at: dict = {}
    for i, event in enumerate(events):
        if event[0] == "send":
            if owed:
                return True
            sent_at[event[1]] = i
        elif event[0] == "hold":
            owed = False
            last_hold = i
        elif event[1] in dm.TILE_BACKPRESSURE_STATUSES and sent_at[event[2]] > last_hold:
            owed = True
    return False


@pytest.mark.parametrize("seed", range(150))
def test_jittered_crawls_hold_every_refusal_and_keep_their_cap(
    monkeypatch, tmp_path, limiters, seed
):
    """Round-7 review, as a seeded sweep over overlapping responses: fresh and
    resumed crawls, stuck tiles, storms starting anywhere, caps from tight to
    none. Whatever the outcome, (a) no request follows a 429/503 without a
    hold first -- a stopping crawl used to skip its hold while tiles already
    admitted under the cap still went out -- (b) every request is counted,
    (c) a cap is never passed (no transport errors here, so no residue), and
    (d) a storm on every remaining request ends as 87, never a census."""
    import random

    rng = random.Random(seed)
    events: list = []

    async def record_hold(seconds):
        events.append(("hold", seconds))

    monkeypatch.setattr(dm, "_backpressure_sleep", record_hold)
    cp = str(tmp_path / "cp")
    n_recorded = rng.choice([0, 1, 3, 6])
    recorded = [_TILES[i] for i in sorted(rng.sample(range(40), n_recorded))]
    fresh = _seed_resume(cp, rng.choice([2, 5, 30]), recorded) if recorded else list(_TILES)
    # Early in request order half the time, where a tight cap meets them
    # (the round-8 layouts).
    stuck = list(rng.sample(fresh[:12] if rng.random() < 0.5 else fresh, min(2, len(fresh))))
    script = {t: [status(503)] * 10 for t in recorded + stuck}
    storm_at = rng.choice(["none", "none", "first", "recorded"])
    storm_from = {"none": (), "first": fresh[:1], "recorded": recorded[:1]}[storm_at]
    max_requests = rng.choice([None, 3, 8, 20])
    sessions = []

    def build(**kwargs):
        session = _JitteredSession(script, rng, events, storm_from=storm_from, **kwargs)
        sessions.append(session)
        return session

    monkeypatch.setattr(dm.aiohttp, "ClientSession", build)
    try:
        outcome = asyncio.run(
            dm.fetch_city_images_async(
                "Test City",
                _BBOX,
                "MLY|test|token",
                connection_limit=rng.choice([1, 5]),
                checkpoint_path=cp,
                max_requests=max_requests,
            )
        )
    except DownloadError as e:
        outcome = e
    urls = sessions[0].urls
    spent = outcome.api_requests if isinstance(outcome, BaseException) else outcome["api_requests"]
    assert spent == len(urls)
    assert not _sent_after_unheld_refusal(events), events
    if max_requests is not None:
        assert len(urls) <= max_requests
    if storm_from and max_requests is None:
        assert type(outcome) is HostThrottledError, outcome


@pytest.mark.parametrize("seed", range(150))
def test_jittered_crawls_with_transport_errors_hold_every_refusal(
    monkeypatch, tmp_path, limiters, seed
):
    """Round-9 review, as a sweep: the round-7 sweep above has no 5xx, so it
    never meets a `@backoff` retry -- a request that holds no reservation
    and can go out after a stopping crawl decided nothing more would. Here
    500/502/504s and 429s land anywhere, under caps from tight to none and
    with or without a storm. No request may follow a 429/503 unheld, every
    request is counted, and a cap is passed by no more than the retries of
    tiles already in flight (the documented residue)."""
    import random

    import backoff._async as backoff_async

    rng = random.Random(seed)
    events: list = []

    async def record_hold(seconds):
        events.append(("hold", seconds))

    real_sleep = asyncio.sleep

    async def jittered_backoff(delay, *args, **kwargs):
        for _ in range(rng.choice([0, 1, 3, 6])):
            await real_sleep(0)

    monkeypatch.setattr(dm, "_backpressure_sleep", record_hold)
    monkeypatch.setattr(backoff_async.asyncio, "sleep", jittered_backoff)
    connection_limit = rng.choice([1, 2, 3, 5])
    max_requests = rng.choice([None, 2, 3, 5, 8, 20, 60])
    script = {}
    for t in rng.sample(_TILES[:80], rng.choice([2, 5, 10])):
        script[t] = [status(rng.choice([500, 502, 504]))] * rng.choice([1, 2, 4]) + [ok()]
    for t in rng.sample(_TILES[:80], rng.choice([0, 1, 2])):
        script[t] = [status(429, **{"Retry-After": rng.choice(["60", "300"])}), ok()]
    storm_from = (_TILES[rng.randrange(40)],) if rng.random() < 0.3 else ()
    sessions = []

    def build(**kwargs):
        session = _JitteredSession(script, rng, events, storm_from=storm_from, **kwargs)
        sessions.append(session)
        return session

    monkeypatch.setattr(dm.aiohttp, "ClientSession", build)
    try:
        outcome = asyncio.run(
            dm.fetch_city_images_async(
                "Test City",
                _BBOX,
                "MLY|test|token",
                connection_limit=connection_limit,
                checkpoint_path=str(tmp_path / "cp"),
                max_requests=max_requests,
            )
        )
    except DownloadError as e:
        outcome = e
    urls = sessions[0].urls
    spent = outcome.api_requests if isinstance(outcome, BaseException) else outcome["api_requests"]
    assert spent == len(urls)
    assert not _sent_after_unheld_refusal(events), events
    if max_requests is not None:
        assert len(urls) <= max_requests + connection_limit * (dm.TILE_MAX_TRIES - 1)


def test_a_malformed_stuck_record_costs_nothing_but_the_record(tmp_path):
    """Round-5 nit: a bad `stuck_tiles` entry must not discard the checkpoint."""
    cp = str(tmp_path / "cp")
    _seed_resume(cp, 10, [])
    path = dm._state_path(cp)
    with open(path, encoding="utf-8") as f:
        state = json.load(f)
    state["stuck_tiles"] = [["a", "b"], [1]]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)
    loaded = dm.load_tile_checkpoint(cp, bbox=_BBOX, tiles=_TILES)
    assert loaded is not None and loaded.stuck == set()
    assert len(loaded.done) == len(_TILES) - 10


def test_a_checkpoint_without_a_stuck_record_still_resumes(monkeypatch, tmp_path, limiters, holds):
    """A commit record written before round 4 has no `stuck_tiles`; it reads
    as none known, never as an unusable checkpoint (which would discard the
    night's tiles), and one the checkpoint has since committed is dropped."""
    cp = str(tmp_path / "cp")
    first, _ = _crawl(monkeypatch, {}, connection_limit=1, checkpoint_path=cp, max_requests=20)
    assert type(first) is SweepIncompleteError, first
    path = dm._state_path(cp)
    with open(path, encoding="utf-8") as f:
        state = json.load(f)
    assert state["stuck_tiles"] == []
    del state["stuck_tiles"]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)
    loaded = dm.load_tile_checkpoint(cp, bbox=_BBOX, tiles=_TILES)
    assert loaded is not None and loaded.stuck == set()
    assert len(loaded.done) == first.units_done

    # A recorded tile the checkpoint has since committed is dropped on load.
    state["stuck_tiles"] = [list(next(iter(loaded.done)))]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)
    assert dm.load_tile_checkpoint(cp, bbox=_BBOX, tiles=_TILES).stuck == set()


def test_a_tile_refused_then_served_is_not_recorded_as_stuck(
    monkeypatch, tmp_path, limiters, holds
):
    """The record is "refused and NOT since served": a throttle the tile's own
    retry got through is no reason to send that tile last tomorrow, while one
    still unserved when the crawl stopped is."""
    cp = str(tmp_path / "cp")
    served, unserved = _TILES[3], _TILES[15]
    error, _ = _crawl(
        monkeypatch,
        {served: [status(429), ok()], unserved: [status(503)] * 50},
        connection_limit=1,
        checkpoint_path=cp,
        max_requests=20,
    )
    assert type(error) is SweepIncompleteError, error
    with open(dm._state_path(cp), encoding="utf-8") as f:
        assert [tuple(t) for t in json.load(f)["stuck_tiles"]] == [unserved]


def test_a_throttle_whose_retries_succeed_still_pauses_with_tiles_served_between(
    monkeypatch, tmp_path, limiters, holds, caplog
):
    """The other half of the withdrawal rule: a tile refused, held and then
    SERVED confirms its episode as a real throttle, and a confirmed strike is
    never withdrawn by other tiles being served. Five sockets interleave served
    tiles between the three 429s, so at each 429 the earlier strikes are still
    pending and do not count (episode 1/3 each time) -- and the crawl must
    still stop as 87 when their retries succeed and confirm them."""
    caplog.set_level(logging.INFO, logger=dm.__name__)
    script = {t: [status(429), ok()] for t in (_TILES[0], _TILES[2], _TILES[4])}
    error, session = _crawl(
        monkeypatch, script, connection_limit=5, checkpoint_path=str(tmp_path / "cp")
    )
    assert type(error) is HostThrottledError, error
    assert host_exit_code(error) == _THROTTLED
    assert "3 HTTP 429/503 episodes" in str(error)
    # Every 429-answered tile was retried and served, so none is failed and
    # the confirming tiles are committed for the resume.
    for t in (_TILES[0], _TILES[2], _TILES[4]):
        assert sum(f"/{t[0]}/{t[1]}?" in u for u in session.urls) == 2
    messages = [r.getMessage() for r in caplog.records]
    assert not any("episode 3/3" in m for m in messages)
    assert error.api_requests == len(session.urls)


def test_responses_inside_one_hold_are_one_episode_not_three_strikes(
    monkeypatch, tmp_path, limiters, holds, caplog
):
    """Review of #444: one throttle window can answer several in-flight
    requests at once. Three tiles sent together and all answered 429 are ONE
    episode -- one strike, one halving -- so the crawl retries and completes
    instead of pausing on what was a single "slow down"."""
    slow_429 = []
    for _ in range(3):
        r = status(429)
        r.delay = 0.01
        slow_429.append(r)
    script = {xy: [slow, ok()] for xy, slow in zip(_TILES[:3], slow_429, strict=True)}
    # A LATER, separate throttle: the second episode. Counted right, the
    # crawl is at 2 of 3 and carries on; had the first episode's three
    # responses each been a strike, this would be the fourth and pause it.
    script[_TILES[200]] = [status(429), ok()]
    caplog.set_level(logging.INFO, logger=dm.__name__)
    # Three sockets: tiles 0-2 are the only requests in flight until their
    # slow 429s land, so tile 200 is sent only after the first hold began.
    result, session = _crawl(
        monkeypatch, script, connection_limit=3, checkpoint_path=str(tmp_path / "cp")
    )

    assert not isinstance(result, BaseException), result
    assert result["failed_tiles"] == []
    assert result["api_requests"] == len(session.urls) == len(_TILES) + 4
    messages = [r.getMessage() for r in caplog.records]
    assert sum("episode 1/3" in m for m in messages) == 1
    assert sum("episode 2/3" in m for m in messages) == 1
    assert not any("pausing the crawl" in m for m in messages)
    (limiter,) = limiters
    assert limiter.slowdowns == [2.0]


# ── A refusal ends a crawl at once, backpressure or not (review of #444) ───

_LOGIN = {"Location": "https://www.mapillary.com/login/?next=x"}


def test_no_request_goes_out_after_a_302(monkeypatch, limiters):
    """Requests already waiting on the pacer when a 302 lands used to be sent
    anyway (3-4 measured). The refusal closes the pacer, so the count at the
    moment the 302 is answered is the crawl's last."""
    real_sleep = asyncio.sleep

    async def slow_acquire(self):
        await real_sleep(0.002)

    monkeypatch.setattr(_RecordingLimiter, "acquire", slow_acquire)
    refusal = status(302, **_LOGIN)
    refusal.delay = 0.005
    sessions = []

    def build(**kwargs):
        sessions.append(_ScriptedSession({_TILES[2]: [refusal]}, **kwargs))
        return sessions[-1]

    monkeypatch.setattr(dm.aiohttp, "ClientSession", build)
    sent_at_refusal = []
    real_record = dm.TileStatusCounts.record

    def record(self, code, retry_after):
        if code == 302:
            sent_at_refusal.append(len(sessions[0].urls))
        return real_record(self, code, retry_after)

    monkeypatch.setattr(dm.TileStatusCounts, "record", record)
    with pytest.raises(HostBlockedError) as excinfo:
        asyncio.run(
            dm.fetch_city_images_async("Test City", _BBOX, "MLY|test|token", connection_limit=5)
        )
    assert sent_at_refusal, "the 302 was never answered"
    assert len(sessions[0].urls) == sent_at_refusal[0], "a request went out after the 302"
    assert excinfo.value.api_requests == len(sessions[0].urls)


def test_a_302_during_a_429_hold_ends_the_hold_and_sends_nothing_more(monkeypatch, limiters):
    """Tile 0's 429 opens a hold; tile 1's 302, already in flight, lands
    during it. The crawl must exit 75 now -- not sleep out the hold holding the
    host lock, and not release the tiles waiting behind it. Turned-away tiles
    set `capped`, so this also pins that the `fatal` raise runs first."""
    real_sleep = asyncio.sleep
    hold_log = {"started": 0, "finished": 0}

    async def long_hold(seconds):
        hold_log["started"] += 1
        await real_sleep(5)  # cut short, or the test is visibly slow and fails
        hold_log["finished"] += 1

    monkeypatch.setattr(dm, "_backpressure_sleep", long_hold)
    # Both responses take time, so both requests are in flight together.
    throttle = status(429)
    throttle.delay = 0.01
    refusal = status(302, **_LOGIN)
    refusal.delay = 0.05
    script = {_TILES[0]: [throttle], _TILES[1]: [refusal]}
    loop_time = []

    def timed_crawl():
        import time as _time

        t0 = _time.monotonic()
        outcome, session = _crawl(monkeypatch, script, connection_limit=2)
        loop_time.append(_time.monotonic() - t0)
        return outcome, session

    error, session = timed_crawl()
    assert isinstance(error, HostBlockedError), error
    assert host_exit_code(error) == 75
    assert hold_log == {"started": 1, "finished": 0}, "the hold was slept out"
    assert loop_time[0] < 2.0
    assert len(session.urls) == error.api_requests == 2


def test_a_302_beside_a_backpressure_pause_is_still_exit_75(monkeypatch, tmp_path, limiters, holds):
    """Both in one crawl: tile 0's Retry-After past the ceiling pauses the
    crawl, then tile 1's 302 (in flight) lands. The refusal wins -- exit 75,
    which records the refusal -- never the throttled 87."""
    throttle = status(429, **{"Retry-After": "3600"})
    throttle.delay = 0.01
    refusal = status(302, **_LOGIN)
    refusal.delay = 0.05
    script = {_TILES[0]: [throttle], _TILES[1]: [refusal]}
    error, session = _crawl(
        monkeypatch, script, connection_limit=2, checkpoint_path=str(tmp_path / "cp")
    )
    assert type(error) is HostBlockedError, error
    assert host_exit_code(error) == 75
    assert len(session.urls) == 2


def test_a_429_after_a_302_neither_holds_nor_pauses(monkeypatch, limiters, holds, caplog):
    """The 302 has already ended the crawl; a 429 to a request that was in
    flight beside it must not open a hold or announce a backpressure stop."""
    caplog.set_level(logging.INFO, logger=dm.__name__)
    refusal = status(302, **_LOGIN)
    refusal.delay = 0.01
    # Past the ceiling: without the guard this is the one 429 that would
    # still announce a stop, since the closed pacer already skips a hold.
    throttle = status(429, **{"Retry-After": "3600"})
    throttle.delay = 0.05
    script = {_TILES[0]: [refusal], _TILES[1]: [throttle]}
    error, session = _crawl(monkeypatch, script, connection_limit=2)
    assert type(error) is HostBlockedError, error
    assert holds == []
    messages = [r.getMessage() for r in caplog.records]
    assert not any("holding every tile request" in m for m in messages)
    assert not any("pausing the crawl" in m for m in messages)
    assert len(session.urls) == 2
