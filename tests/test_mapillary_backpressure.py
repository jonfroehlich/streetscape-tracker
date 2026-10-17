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
    for unreadable in (None, "", "soon", "-5", "1.5"):
        assert dm.retry_after_seconds(unreadable, now) is None, unreadable


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


def test_the_third_429_pauses_the_crawl_with_exit_83_not_a_refusal(
    monkeypatch, tmp_path, limiters, holds, caplog
):
    caplog.set_level(logging.INFO, logger=dm.__name__)
    error, session = _crawl(
        monkeypatch, _three_429s(), connection_limit=1, checkpoint_path=str(tmp_path / "cp")
    )

    assert isinstance(error, SweepIncompleteError), error
    assert not isinstance(error, HostBlockedError)
    from streetscape_metadata_tracker.download_common import (
        SWEEP_STOP_BACKPRESSURE,
        SWEEP_STOP_PHRASES,
    )

    assert error.stopped_by == SWEEP_STOP_BACKPRESSURE
    assert error.stop_phrase == SWEEP_STOP_PHRASES[SWEEP_STOP_BACKPRESSURE]
    assert "backpressure" in str(error)
    # Tiles 0 and 1 were retried and committed; tile 2 was not.
    assert error.units_done == 2
    assert error.unit_count == len(_TILES)
    # Two holds; the third response pauses instead of waiting.
    assert holds == [pytest.approx(60.0, abs=1.0)] * 2
    # 2 + 2 + 1 requests, each counted once, on both counters.
    assert error.api_requests == len(session.urls) == 5
    assert error.api_requests_total == 5
    (limiter,) = limiters
    assert limiter.slowdowns == [2.0], "halved ONCE per crawl, not per response"
    (line,) = _status_lines(caplog)
    assert " 429=3 " in line and " requests=5 retries=2 " in line


def test_the_backpressure_pause_phrase_is_what_the_scheduler_reads_back(
    monkeypatch, tmp_path, limiters, holds
):
    from streetscape_metadata_tracker.scheduler import _pause_stop_phrase

    error, _ = _crawl(
        monkeypatch, _three_429s(), connection_limit=1, checkpoint_path=str(tmp_path / "cp")
    )
    # The CLIs' pause line, as cli.py and collect.py format it.
    line = (
        f"mapillary crawl paused at {error.units_done}/{error.unit_count} {error.unit_name} "
        f"({error.stop_phrase}); re-run to resume from {error.checkpoint_path}"
    )
    assert _pause_stop_phrase(line) == error.stop_phrase


def test_a_retry_after_past_the_ceiling_pauses_at_once_without_waiting(
    monkeypatch, tmp_path, limiters, holds
):
    """Retrying before the time the server named would disobey it, so the
    crawl does not wait the ceiling and ask early: it pauses for tomorrow."""
    script = {_TILES[0]: [status(429, **{"Retry-After": "3600"})]}
    error, session = _crawl(
        monkeypatch, script, connection_limit=1, checkpoint_path=str(tmp_path / "cp")
    )
    # Nothing committed, so this is the cap's no-checkpoint arm: a plain
    # DownloadError, never "re-run to resume" over an empty directory.
    assert type(error) is DownloadError
    assert holds == []
    assert len(session.urls) == error.api_requests == 1


def test_a_retry_after_past_the_ceiling_pauses_a_crawl_with_progress(
    monkeypatch, tmp_path, limiters, holds
):
    script = {_TILES[3]: [status(429, **{"Retry-After": "3600"})]}
    error, session = _crawl(
        monkeypatch, script, connection_limit=1, checkpoint_path=str(tmp_path / "cp")
    )
    assert isinstance(error, SweepIncompleteError)
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
    assert isinstance(paused, SweepIncompleteError)

    resumed, session = _crawl(monkeypatch, {}, checkpoint_path=cp)
    assert not isinstance(resumed, BaseException), resumed
    assert resumed["api_requests"] == len(session.urls) == len(_TILES) - 2
    assert resumed["api_requests_total"] == 5 + len(_TILES) - 2
    assert resumed["failed_tiles"] == []


def test_a_hold_that_would_outlast_the_deadline_pauses_instead(
    monkeypatch, tmp_path, limiters, holds
):
    """The scheduler's SIGKILL lands 600 s after the forwarded budget, and a
    hold can be 900 s: one begun near the deadline must pause (exit 83), not
    sleep into the kill (a counted failure)."""
    import time as _time

    script = {_TILES[3]: [status(429, **{"Retry-After": "300"})]}
    error, session = _crawl(
        monkeypatch,
        script,
        connection_limit=1,
        checkpoint_path=str(tmp_path / "cp"),
        deadline_monotonic=_time.monotonic() + 200,
    )
    assert isinstance(error, SweepIncompleteError), error
    assert error.stopped_by == "backpressure"
    assert "wall-clock budget" in str(error)
    assert holds == []
    assert error.units_done == 3
    assert len(session.urls) == 4
