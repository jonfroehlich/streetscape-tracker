"""Overpass hardening (issue #209).

`overpass-api.de` firewall-banned makelab2's IP on 2026-08-14 and every road
walk died in osmnx's `_download_graph` as a bare `tenacity.RetryError`, after
minutes of retries, with nothing in the alert email naming Overpass at all.

These tests pin the four things that made that failure expensive rather than
informative: a timeout setting that had never applied, a retry policy that
retried settled answers, no typed failure to act on, and an unbounded hang.

No network: `requests.get` / `ox.graph_from_bbox` are stubbed. Note conftest's
autouse `_no_overpass_status_probe` stubs the pre-flight suite-wide, so the
tests here that care about it re-stub `requests.get` and call the real probe.
DNS is faked module-wide too (`_no_real_dns`): since #366 the real probe
resolves the host itself, through `download_common._pinned_like_osmnx`.
"""

import socket
import time

import networkx as nx
import osmnx as ox
import pytest
import requests
from osmnx._errors import InsufficientResponseError, ResponseStatusCodeError

from streetscape_metadata_tracker import download_common as dc
from streetscape_metadata_tracker.download_common import (
    HOST_OVERPASS,
    DownloadError,
    HostBlockedError,
    HostUnavailableError,
)
from streetscape_street_analyzer import download_street_network as dsn
from tests import _dns_fakes

# Captured at import, which happens BEFORE conftest's autouse
# `_no_overpass_status_probe` replaces the module attribute. The tests below
# that exercise the probe itself call this rather than `dsn._overpass_refusing`,
# which would otherwise be the suite-wide stub.
_REAL_PROBE = dsn._overpass_refusing


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch):
    """The real pre-flight resolves the Overpass host itself (issue #366), so
    every test here that calls `_REAL_PROBE` would otherwise make a real DNS
    lookup -- network access, and offline a `gaierror` that turns each
    "refusing" assertion into a silent can't-tell. Installed for the whole
    module; the recorded lookups are returned for the tests that read them."""
    return _dns_fakes.install(monkeypatch)


# ---------------------------------------------------------------------------
# The dead setting
# ---------------------------------------------------------------------------


def test_the_timeout_we_set_is_the_one_osmnx_actually_reads():
    """
    `ox.settings.timeout = 60` sat here for a year doing nothing: osmnx 2.x
    renamed it, and `ox.settings` is a plain module, so the assignment created
    an attribute no code reads and every call ran at the 180s default.
    """
    assert ox.settings.requests_timeout == dsn.OVERPASS_TIMEOUT_S
    # If a future osmnx reintroduces `timeout`, this must be revisited rather
    # than silently having two settings that disagree.
    assert not hasattr(ox.settings, "timeout")


def test_the_server_side_query_timeout_tracks_the_same_value():
    """
    Why the fix keeps 180 instead of restoring the intended 60: osmnx
    interpolates requests_timeout into the Overpass QL `[timeout:N]` header, so
    lowering it would start SERVER-aborting the large-bbox fetches that succeed
    today. Reads an osmnx internal deliberately — it pins the coupling that
    makes the obvious "fix" a regression.
    """
    assert f"[timeout:{dsn.OVERPASS_TIMEOUT_S}]" in ox._overpass._make_overpass_settings()


def test_we_identify_ourselves_to_overpass():
    """Their usage policy asks for a UA/Referer that uniquely identifies the
    app; osmnx's default names osmnx, which is every osmnx user on the planet."""
    assert "streetscape" in ox.settings.http_user_agent
    assert "OSMnx" not in ox.settings.http_user_agent


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


def test_only_transport_faults_are_retried():
    """The predicate is unchanged by #357, which widened HOW LONG, not WHAT.
    The schedule itself is pinned in tests/test_overpass_retry.py."""
    retryable = dsn._RETRYABLE_OVERPASS
    assert requests.exceptions.ConnectionError in retryable
    assert requests.exceptions.Timeout in retryable
    # A settled answer must not be re-asked: three attempts bought three
    # identical refusals from a host that had already said no.
    assert not any(issubclass(ResponseStatusCodeError, t) for t in retryable)
    assert not any(issubclass(InsufficientResponseError, t) for t in retryable)


def test_a_permanent_error_is_attempted_exactly_once(monkeypatch):
    calls = []

    def boom(**kwargs):
        calls.append(1)
        raise InsufficientResponseError("no drivable ways")

    monkeypatch.setattr(dsn.ox, "graph_from_bbox", boom)
    with pytest.raises(InsufficientResponseError):
        dsn._download_graph_retrying((0, 0, 1, 1), "drive", dsn.OverpassRetryPolicy())
    assert len(calls) == 1


def test_a_transport_fault_is_retried_and_its_cause_survives(monkeypatch):
    calls = []

    def boom(**kwargs):
        calls.append(1)
        raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(dsn.ox, "graph_from_bbox", boom)
    # conftest's autouse stub makes the waits no-ops and the clock never moves,
    # so the attempt cap is what binds here.
    with pytest.raises(HostBlockedError) as excinfo:
        dsn._download_graph_named((0, 0, 1, 1), "drive")
    assert len(calls) == dsn.OverpassRetryPolicy().max_attempts
    # The real exception is one hop away, never a tenacity-style RetryError.
    assert isinstance(excinfo.value.__cause__, requests.exceptions.ConnectionError)
    assert "RetryError" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# Typed failures: host-wide vs city-specific
# ---------------------------------------------------------------------------


def test_a_refused_connection_is_named_a_host_block(monkeypatch):
    monkeypatch.setattr(
        dsn,
        "_download_graph",
        lambda bbox, nt, *rest: (_ for _ in ()).throw(
            requests.exceptions.ConnectionError("refused")
        ),
    )
    with pytest.raises(HostBlockedError) as excinfo:
        dsn._download_graph_named((0, 0, 1, 1), "drive")

    assert excinfo.value.host == HOST_OVERPASS
    text = str(excinfo.value)
    assert "per-IP" in text, "must say a different credential would not help"
    assert "OVERPASS_URL" in text, "must name the escape hatch"
    # The old symptom must not be how this reads any more.
    assert "RetryError" not in text


def test_a_ban_page_is_a_host_block(monkeypatch):
    monkeypatch.setattr(
        dsn,
        "_download_graph",
        lambda bbox, nt, *rest: (_ for _ in ()).throw(ResponseStatusCodeError("403 Forbidden")),
    )
    with pytest.raises(HostBlockedError) as excinfo:
        dsn._download_graph_named((0, 0, 1, 1), "drive")
    assert excinfo.value.host == HOST_OVERPASS


def test_an_empty_bbox_is_a_city_failure_not_a_host_block(monkeypatch):
    """
    The distinction the whole night depends on: a village with no drivable ways
    is a permanent fact about THAT CITY. Typing it host-wide would trip the #208
    breaker and abandon the other 19 cities over one roadless bbox.
    """
    monkeypatch.setattr(
        dsn,
        "_download_graph",
        lambda bbox, nt, *rest: (_ for _ in ()).throw(InsufficientResponseError("no ways")),
    )
    with pytest.raises(DownloadError) as excinfo:
        dsn._download_graph_named((0, 0, 1, 1), "drive")

    assert not isinstance(excinfo.value, HostUnavailableError)
    assert "not this host" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The /status pre-flight
# ---------------------------------------------------------------------------


class _FakeStatus:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


_HEALTHY = "Connected as: 403941390\nRate limit: 2\n2 slots available now.\n"


def _counting(reply):
    """A `requests.get` stub that counts its calls, so a test asserting the
    probe's None can also show the GET was really made -- None is ALSO what a
    lookup failure short-circuiting before the GET returns (#366)."""

    def get(*a, **k):
        get.calls += 1
        return reply()

    get.calls = 0
    return get


def test_the_probe_passes_a_healthy_instance(monkeypatch):
    get = _counting(lambda: _FakeStatus(200, _HEALTHY))
    monkeypatch.setattr(dsn.requests, "get", get)
    assert _REAL_PROBE() is None
    assert get.calls == 1


def test_the_probe_names_a_refusing_instance(monkeypatch):
    monkeypatch.setattr(dsn.requests, "get", lambda *a, **k: _FakeStatus(429, "rate limited"))
    assert "429" in _REAL_PROBE()


def test_the_probe_treats_a_server_error_as_unknown_not_a_refusal(monkeypatch):
    """
    The asymmetry that governs this whole function: a false negative costs one
    wasted fetch that produces the real error anyway, while a false positive
    skips every street channel of every city for the night. A 502/503 from a
    front end while /interpreter is healthy is ordinary on a volunteer-run
    instance — so refusal is an allow-list, not "anything that isn't 200".
    """
    for code in (500, 502, 503, 504, 406, 404):
        get = _counting(lambda c=code: _FakeStatus(c, "nope"))
        monkeypatch.setattr(dsn.requests, "get", get)
        assert _REAL_PROBE() is None, f"HTTP {code} must not skip the night"
        assert get.calls == 1, f"HTTP {code} was never asked"


def test_a_queued_slot_is_not_a_refusal(monkeypatch):
    """
    Overpass grants 2 slots per IP and reports a wait when both are in use.
    osmnx reads the same endpoint in `_get_overpass_pause` and sleeps the wait
    off, so cancelling here would cancel a fetch that was going to succeed —
    and being briefly queued behind our own previous query is the normal state
    of a working night.
    """
    for seconds in (5, 120, 600, 3600):
        queued = f"Slot available after: 2026-08-15T18:00:00Z, in {seconds} seconds."
        get = _counting(lambda t=queued: _FakeStatus(200, t))
        monkeypatch.setattr(dsn.requests, "get", get)
        assert _REAL_PROBE() is None
        assert get.calls == 1


def test_the_probe_survives_an_osmnx_internal_going_away(monkeypatch):
    """
    `ox._http._get_http_headers` is private and requirements.txt pins
    `osmnx>=2.0` with no ceiling. An AttributeError here would fire inside the
    host lock, before any real request, and fail every street collection on the
    machine — from a pre-flight whose entire contract is "advisory".
    """

    def gone(*a, **k):
        raise AttributeError("module 'osmnx._http' has no attribute '_get_http_headers'")

    monkeypatch.setattr(dsn.ox._http, "_get_http_headers", gone)
    assert _REAL_PROBE() is None


def test_the_probe_sends_our_user_agent_not_the_requests_default(monkeypatch):
    """
    Measured 2026-08-15: overpass-api.de answers HTTP 406 to the stock
    `python-requests/x.y.z` User-Agent, and 200 to anything else. A probe using
    the default would have read that 406 as a refusal and skipped EVERY city of
    every night — a self-inflicted outage in the code meant to prevent one.
    """
    seen = {}

    def capture(url, **kwargs):
        seen.update(kwargs.get("headers") or {})
        return _FakeStatus(200, _HEALTHY)

    monkeypatch.setattr(dsn.requests, "get", capture)
    _REAL_PROBE()
    assert "streetscape" in seen.get("User-Agent", "")


def test_the_probe_never_fails_a_healthy_fetch(monkeypatch):
    """Advisory only. If the probe itself cannot connect, or the format changes
    under us, we proceed and let the real request produce the real error."""

    def cannot_connect():
        raise requests.exceptions.ConnectionError("probe cannot connect")

    unreachable = _counting(cannot_connect)
    monkeypatch.setattr(dsn.requests, "get", unreachable)
    assert _REAL_PROBE() is None
    assert unreachable.calls == 1

    changed = _counting(lambda: _FakeStatus(200, "something new"))
    monkeypatch.setattr(dsn.requests, "get", changed)
    assert _REAL_PROBE() is None
    assert changed.calls == 1


class _StatusEndpoint:
    """In-memory /api/status that records where each GET would have connected."""

    def __init__(self, reply=None):
        self.reply = reply or (lambda: _FakeStatus(200, _HEALTHY))
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append(
            {
                "url": url,
                "connect_to": _dns_fakes.connect_address(url),
                "pin_held": dc._PIN_LOCK.locked(),
            }
        )
        return self.reply()


@pytest.fixture
def status_endpoint(monkeypatch):
    fake = _StatusEndpoint()
    monkeypatch.setattr(dsn.requests, "get", fake)
    monkeypatch.setattr(ox.settings, "overpass_url", dc.DEFAULT_OVERPASS_URL)
    return fake


def test_the_probe_connects_to_the_ipv4_address_osmnx_would_pin(status_endpoint, _no_real_dns):
    """Issue #366. osmnx resolves the host with gethostbyname (one address,
    IPv4 only) and pins its query there; plain requests on a dual-stack host
    would take the IPv6 answer -- and Overpass identifies a client by its IPv4
    address or its IPv6 /64, so an unpinned pre-flight can be asking about a
    different client from the one the walk is. A false "refusing" from that
    other client latches Overpass for the night and strands the city (#341)."""
    assert _REAL_PROBE() is None
    assert _no_real_dns == ["overpass-api.de"]
    assert status_endpoint.calls[0]["connect_to"] == _dns_fakes.V4
    # Only the address is pinned: the URL, and so SNI and the Host header,
    # still name the host.
    assert status_endpoint.calls[0]["url"] == "https://overpass-api.de/api/status"


def test_the_probe_and_the_query_connect_to_the_same_address(status_endpoint):
    """Under one fake resolver, the pre-flight lands where osmnx's own
    `_config_dns` sends the query. That is ALL this pins -- nothing offline can
    compare the two against real DNS -- but it is what makes the pre-flight's
    answer about the query's client rather than a neighbour's."""
    assert _REAL_PROBE() is None
    ox._http._config_dns(dc.DEFAULT_OVERPASS_URL)  # patches socket.getaddrinfo
    osmnx_addr = _dns_fakes.connect_address(dc.DEFAULT_OVERPASS_URL)
    assert status_endpoint.calls[0]["connect_to"] == osmnx_addr == _dns_fakes.V4


def test_the_pin_follows_the_endpoint_the_probe_asks(status_endpoint, _no_real_dns, monkeypatch):
    """Both an explicit url and osmnx's configured one (where the OVERPASS_URL
    mirror lands) are resolved and pinned -- not the default host."""
    assert _REAL_PROBE("https://overpass.example.org/api") is None
    monkeypatch.setattr(ox.settings, "overpass_url", "https://mirror.example.net/api")
    assert _REAL_PROBE() is None
    assert _no_real_dns == ["overpass.example.org", "mirror.example.net"]
    assert [c["connect_to"] for c in status_endpoint.calls] == [_dns_fakes.OTHER_V4] * 2


def test_the_pin_is_held_for_the_get_and_gone_after_it(status_endpoint):
    """The pin is taken ON the pre-flight's GET and released on every path, or
    one failed pre-flight would leave `getaddrinfo` patched (or `_PIN_LOCK`
    held) for the rest of the child."""
    assert _REAL_PROBE() is None
    assert status_endpoint.calls[0]["pin_held"] is True
    assert socket.getaddrinfo is _dns_fakes.dual_stack_getaddrinfo
    assert not dc._PIN_LOCK.locked()

    def boom():
        raise requests.exceptions.ConnectionError("[Errno 111] Connection refused")

    status_endpoint.reply = boom
    assert _REAL_PROBE() is None
    assert socket.getaddrinfo is _dns_fakes.dual_stack_getaddrinfo
    assert not dc._PIN_LOCK.locked()

    status_endpoint.reply = lambda: _FakeStatus(429, "rate limited")
    assert "429" in _REAL_PROBE()
    assert status_endpoint.calls[-1]["connect_to"] == _dns_fakes.V4
    assert not dc._PIN_LOCK.locked()


def _osmnx_query_address(url: str) -> str:
    """Where osmnx's query to ``url`` would connect: its own ``_config_dns``
    pin (which matches the host AS WRITTEN in the URL), then urllib3's lookup
    (which always asks for the host lowercased)."""
    ox._http._config_dns(url)  # patches socket.getaddrinfo; monkeypatch restores it
    return _dns_fakes.connect_address(url)


def test_the_fixture_sees_osmnx_miss_a_mixed_case_host(status_endpoint):
    """Control for the next test: handed a mixed-case URL raw, osmnx's pin
    compares ``Overpass.Example.org`` against the lowercase host urllib3 asks
    for, never matches, and its query takes the IPv6 answer. If this ever
    passes by pinning, osmnx fixed it and the normalization is belt-and-braces."""
    assert _osmnx_query_address("https://Overpass.Example.org/api") == _dns_fakes.V6


def test_a_mixed_case_mirror_is_one_client_for_the_probe_and_the_query(
    status_endpoint, monkeypatch
):
    """Review of #366: `urlsplit().hostname` lowercases, osmnx's
    `_hostname_from_url` does not. Unnormalized, a mixed-case OVERPASS_URL had
    the pre-flight pinned to IPv4 and osmnx's query unpinned -- the #366
    mismatch in reverse. Lowercasing where the URL is configured makes both
    land on the same address."""
    monkeypatch.setenv(dsn.OVERPASS_URL_ENV, "https://Overpass.Example.org/api")
    dsn._apply_overpass_url()
    assert ox.settings.overpass_url == "https://overpass.example.org/api"

    assert _REAL_PROBE() is None
    probe_addr = status_endpoint.calls[0]["connect_to"]
    assert probe_addr == _osmnx_query_address(ox.settings.overpass_url) == _dns_fakes.OTHER_V4


def test_normalizing_the_url_touches_only_the_host():
    """Hostnames are case-insensitive; paths are not."""
    assert (
        dc.normalize_overpass_url("https://Overpass-API.DE:8443/API/Interp")
        == "https://overpass-api.de:8443/API/Interp"
    )
    assert dc.normalize_overpass_url(dc.DEFAULT_OVERPASS_URL) == dc.DEFAULT_OVERPASS_URL


def test_a_failed_lookup_is_cant_tell_not_refusing(status_endpoint, monkeypatch):
    """The OPPOSITE of the breaker's re-check, deliberately. There a failed
    lookup keeps Overpass latched (fail-closed); here it is one more
    can't-tell, so the walk proceeds and the real fetch resolves the host its
    own way (osmnx falls back to DNS-over-HTTPS). Nothing is sent."""

    def no_dns(host):
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(socket, "gethostbyname", no_dns)
    assert _REAL_PROBE() is None
    assert status_endpoint.calls == []
    assert socket.getaddrinfo is _dns_fakes.dual_stack_getaddrinfo
    assert not dc._PIN_LOCK.locked()


def test_a_refusing_probe_stops_the_fetch_before_any_query(monkeypatch, tmp_path):
    """The point of the pre-flight: name it in ~1s instead of after the whole
    retry window (issue #357), having issued nothing."""
    from tests.test_host_lock import _city_row

    queried = []
    monkeypatch.setattr(dsn, "_overpass_refusing", lambda url=None: "its status endpoint said 429")
    monkeypatch.setattr(dsn, "_download_graph_named", lambda *a: queried.append(1))

    with pytest.raises(HostBlockedError) as excinfo:
        dsn.fetch_graph(_city_row(), str(tmp_path))

    assert queried == []
    assert excinfo.value.host == HOST_OVERPASS


# ---------------------------------------------------------------------------
# The hang guard
# ---------------------------------------------------------------------------


def test_only_the_alarm_raises_the_deadline_type():
    """
    Builtin TimeoutError IS socket.timeout, so catching it would let a stray
    socket timeout escaping urllib3 be reported as "did not complete within
    900s — most likely repeated 429/504", sending an operator after the wrong
    thing entirely. The alarm gets its own type.
    """
    import socket

    assert issubclass(dsn._DeadlineExceeded, TimeoutError)
    assert socket.timeout is TimeoutError, "the whole hazard: they are the same class"
    assert not isinstance(TimeoutError("connection timed out"), dsn._DeadlineExceeded)


def test_a_stray_socket_timeout_is_not_reported_as_a_hang(monkeypatch):
    monkeypatch.setattr(
        dsn,
        "_download_graph",
        lambda bbox, nt, *rest: (_ for _ in ()).throw(TimeoutError("socket timed out")),
    )
    # Not swallowed into a misleading HostBlockedError — it propagates as itself.
    with pytest.raises(TimeoutError) as excinfo:
        dsn._download_graph_named((0, 0, 1, 1), "drive")
    assert not isinstance(excinfo.value, HostBlockedError)


def test_the_deadline_bound_clears_the_worst_legitimate_fetch():
    """The longest retry window a policy may configure, then one full final
    attempt (a request timeout plus osmnx's pre-request slot pause). Derived,
    so neither raising the window ceiling nor the request timeout can silently
    leave the bound below the thing it has to clear (issue #357) -- and the
    900 s outer limit #357 was asked to keep is still the number."""
    from streetscape_metadata_tracker.overpass_retry import OVERPASS_RETRY_WINDOW_CEILING_S

    assert dsn.OVERPASS_DEADLINE_S >= (
        OVERPASS_RETRY_WINDOW_CEILING_S + dsn.OVERPASS_TIMEOUT_S + dsn.OVERPASS_ATTEMPT_SLACK_S
    )
    assert dsn.OVERPASS_DEADLINE_S == 900


def test_the_deadline_interrupts_a_blocking_call():
    """
    osmnx answers 429/504 by sleeping 55s and recursing with no depth limit, so
    a rate-limit-flavoured refusal never returns on its own. Without a deadline
    the child is SIGKILLed by the scheduler's per-city timeout — and a SIGKILL
    carries no exit code, so the #208 breaker never learns the host is refusing.
    """
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        with dsn._deadline(0.05):
            time.sleep(5)
    assert time.monotonic() - started < 2, "must not have waited out the full sleep"


def test_the_deadline_restores_the_previous_handler():
    import signal

    before = signal.getsignal(signal.SIGALRM)
    with dsn._deadline(30):
        pass
    assert signal.getsignal(signal.SIGALRM) is before


def test_a_hung_fetch_is_reported_as_a_host_block(monkeypatch):
    monkeypatch.setattr(dsn, "OVERPASS_DEADLINE_S", 0.05)
    monkeypatch.setattr(dsn, "_download_graph", lambda bbox, nt: time.sleep(5))
    with pytest.raises(HostBlockedError) as excinfo:
        dsn._download_graph_named((0, 0, 1, 1), "drive")
    assert excinfo.value.host == HOST_OVERPASS
    assert "429/504" in str(excinfo.value)


def test_the_deadline_is_a_no_op_off_the_main_thread():
    """signal.setitimer raises off the main thread; losing the deadline there is
    strictly better than breaking such a caller."""
    import threading

    result = {}

    def run():
        try:
            with dsn._deadline(0.01):
                time.sleep(0.05)
            result["ok"] = True
        except Exception as e:  # pragma: no cover - would be the failure
            result["err"] = e

    t = threading.Thread(target=run)
    t.start()
    t.join()
    assert result.get("ok") is True


# ---------------------------------------------------------------------------
# Mirror override
# ---------------------------------------------------------------------------


def test_a_mirror_url_can_be_set_without_restarting(monkeypatch, tmp_path):
    """
    The incident-time escape hatch, and the reason it is read at call time: it
    is what you reach for at 03:00 when the main instance is refusing this host,
    and an import-time read could neither be exercised by a test nor changed
    without restarting the process.
    """
    from tests.test_host_lock import _city_row

    graph = nx.MultiDiGraph()
    graph.add_edge(1, 2)
    monkeypatch.setattr(dsn, "_download_graph_named", lambda bbox, nt, policy=None: graph)
    monkeypatch.setattr(dsn.ox, "save_graphml", lambda g, p: open(p, "w").close())
    monkeypatch.setattr(ox.settings, "overpass_url", "https://overpass-api.de/api")

    monkeypatch.setenv(dsn.OVERPASS_URL_ENV, "https://overpass.example.org/api")
    dsn.fetch_graph(_city_row(), str(tmp_path))
    assert ox.settings.overpass_url == "https://overpass.example.org/api"


def test_an_unset_mirror_leaves_the_default_alone(monkeypatch):
    monkeypatch.delenv(dsn.OVERPASS_URL_ENV, raising=False)
    monkeypatch.setattr(ox.settings, "overpass_url", "https://overpass-api.de/api")
    dsn._apply_overpass_url()
    assert ox.settings.overpass_url == "https://overpass-api.de/api"


def test_a_successful_fetch_still_works_end_to_end(monkeypatch, tmp_path):
    """Guard against the hardening turning a healthy path into a failure."""
    from tests.test_host_lock import _city_row

    graph = nx.MultiDiGraph()
    graph.add_edge(1, 2)
    monkeypatch.setattr(dsn, "_download_graph", lambda bbox, nt: graph)
    monkeypatch.setattr(dsn.ox, "save_graphml", lambda g, p: open(p, "w").close())

    assert dsn.fetch_graph(_city_row(), str(tmp_path)) is graph


# The breaker's fail-CLOSED reset test, `download_common.overpass_serving`
# (issues #341, #356), is pinned in tests/test_overpass_reset_probe.py.


def test_a_frozen_network_is_written_atomically(monkeypatch, tmp_path):
    """An interrupted in-place write left a truncated GraphML that every later
    reader took for a frozen network -- and the scheduler's frozen-network
    exemption (issue #341) trusts the file's existence. So the save lands in a
    .tmp and is renamed in: a save that dies leaves no network, a save that
    lands leaves no .tmp."""
    import os

    from tests.test_host_lock import _city_row

    graph = nx.MultiDiGraph()
    graph.add_edge(1, 2)
    monkeypatch.setattr(dsn, "_download_graph_named", lambda bbox, nt, policy=None: graph)
    city = _city_row()
    final = dsn.network_cache_path(city.city_id, str(tmp_path))

    def dies(g, p):
        open(p, "w").write("<graphml><node")  # partial
        raise KeyboardInterrupt

    monkeypatch.setattr(dsn.ox, "save_graphml", dies)
    with pytest.raises(KeyboardInterrupt):
        dsn.fetch_graph(city, str(tmp_path))
    assert not os.path.exists(final), "a dead save must not leave a frozen network"

    seen = {}

    def lands(g, p):
        seen["path"] = p
        open(p, "w").close()

    monkeypatch.setattr(dsn.ox, "save_graphml", lands)
    dsn.fetch_graph(city, str(tmp_path))
    assert seen["path"] == final + ".tmp"
    assert os.path.exists(final)
    assert not os.path.exists(final + ".tmp")
