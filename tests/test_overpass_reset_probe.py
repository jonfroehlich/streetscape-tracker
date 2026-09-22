"""The Overpass breaker's RESET test (issues #341, #356).

`download_common.overpass_serving` decides whether a latched Overpass may be
asked again tonight. It must be FAIL-CLOSED -- the one confirmed abuse ban
(2026-08-14) presented as a TCP connection refused, so "unreachable" must
never read as "clear" -- and, since #356, it must measure the endpoint a walk
actually needs. The /status version was fail-closed and correct as written,
and on 2026-09-21 still cleared twice into a host that refused the very next
real query, 4 and 39 minutes later.

No network. `requests.post` is replaced with `_Interpreter`, an in-memory
stand-in for Overpass's /api/interpreter that parses the probe query it is
sent and answers the way the real one does (the shape was measured once from
a laptop on 2026-09-22), and `requests.get` is replaced with a tripwire, so a
probe that drifts back to /status fails loudly instead of passing by accident.
"""

import json
import re
import socket
import urllib.parse

import osmnx as ox
import pytest
import requests

from streetscape_metadata_tracker import download_common as dc
from streetscape_metadata_tracker import scheduler as sched
from streetscape_street_analyzer import download_street_network as dsn

_NONCE = re.compile(r'make probe nonce="([0-9a-f]+)";out;')


def _answer(nonce, *, with_node=True, remark=None, extra=None):
    """The JSON body /api/interpreter returned for the probe on 2026-09-22."""
    elements = [{"type": "node", "id": 1}] if with_node else []
    if nonce is not None:
        elements.append({"type": "probe", "id": 1, "tags": {"nonce": nonce}})
    body = {
        "version": 0.6,
        "generator": "Overpass API 0.7.62.11 87bfad18",
        "osm3s": {"timestamp_osm_base": "2026-09-22T16:46:41Z", "copyright": "ODbL"},
        "elements": elements,
    }
    if remark is not None:
        body["remark"] = remark
    body.update(extra or {})
    return json.dumps(body)


class _Response:
    """Just the surface `overpass_serving` reads: a status and a body."""

    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text

    def json(self):
        # requests raises a ValueError subclass for a non-JSON body.
        return json.loads(self.text)


class _Interpreter:
    """In-memory /api/interpreter. ``reply(nonce)`` -> (status, body text).

    Records every POST so a test can pin the URL, form data, headers and
    timeout it was sent. The default reply is a clean, executed probe.
    """

    def __init__(self, reply=None):
        self.reply = reply or (lambda nonce: (200, _answer(nonce)))
        self.calls = []

    def __call__(self, url, data=None, timeout=None, headers=None, **kwargs):
        # Resolve the host exactly as urllib3 would at connect time, so a test
        # can see which address the request would actually have gone to.
        host = urllib.parse.urlsplit(url).hostname
        sockaddr = socket.getaddrinfo(host, 443, 0, socket.SOCK_STREAM)[0][4]
        self.calls.append(
            {
                "url": url,
                "data": data,
                "timeout": timeout,
                "headers": headers,
                "connect_to": sockaddr[0],
            }
        )
        assert not kwargs, f"unexpected request options {kwargs}"
        match = _NONCE.search((data or {}).get("data", ""))
        status, text = self.reply(match.group(1) if match else None)
        return _Response(status, text)


# A dual-stack host, as makelab2's resolver sees overpass-api.de: getaddrinfo
# prefers an IPv6 address, gethostbyname can only return IPv4. Documentation
# ranges (RFC 3849 / RFC 5737), so nothing here could reach a real server.
_V6 = "2001:db8::2"
_V4 = "192.0.2.52"


def _dual_stack_getaddrinfo(host, port, *args, **kwargs):
    """No-network resolver: an IP literal resolves to itself, a name to IPv6."""
    if host in (_V4, "198.51.100.9"):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port))]
    return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (_V6, port, 0, 0))]


@pytest.fixture
def interpreter(monkeypatch):
    """Install an `_Interpreter` as requests.post, a tripwire as requests.get,
    and a no-network dual-stack resolver in place of the real DNS calls."""
    fake = _Interpreter()

    def no_get(*a, **k):
        raise AssertionError("the reset test issued a GET -- /status is not the reset test (#356)")

    lookups = []

    def gethostbyname(host):
        lookups.append(host)
        return _V4 if host == "overpass-api.de" else "198.51.100.9"

    monkeypatch.setattr(dc.requests, "post", fake)
    monkeypatch.setattr(dc.requests, "get", no_get)
    monkeypatch.setattr(socket, "getaddrinfo", _dual_stack_getaddrinfo)
    monkeypatch.setattr(socket, "gethostbyname", gethostbyname)
    monkeypatch.delenv(dc.OVERPASS_URL_ENV, raising=False)
    fake.lookups = lookups
    return fake


# ---------------------------------------------------------------------------
# What it sends: the interpreter, osmnx's method and headers, the cheapest query
# ---------------------------------------------------------------------------


def test_an_executed_probe_clears_it(interpreter):
    assert dc.overpass_serving() is True
    assert len(interpreter.calls) == 1


def test_it_posts_the_probe_query_to_the_interpreter_not_status(interpreter):
    """The whole of #356: measure the endpoint a walk needs. osmnx POSTs the
    query as the `data` form field to {overpass_url}/interpreter; so does this."""
    assert dc.overpass_serving() is True
    (call,) = interpreter.calls
    assert call["url"] == "https://overpass-api.de/api/interpreter"
    assert call["url"] == dc.DEFAULT_OVERPASS_URL + "/interpreter"
    assert set(call["data"]) == {"data"}
    nonce = _NONCE.search(call["data"]["data"]).group(1)
    assert call["data"]["data"] == dc.OVERPASS_PROBE_QUERY.format(nonce=nonce)


def test_the_probe_query_is_the_cheapest_the_engine_can_be_asked():
    """Pinned verbatim: this is a metered request to a shared volunteer-run
    service. 5 s / 1 MiB declared (vs osmnx's 180 s / 512 MiB), one id lookup,
    no area or bbox clause, and a make-echo to prove it executed. Changing any
    of it is a provider-access decision, not a refactor."""
    assert dc.OVERPASS_PROBE_QUERY == (
        '[out:json][timeout:5][maxsize:1048576];node(1);out ids;make probe nonce="{nonce}";out;'
    )
    query = dc.OVERPASS_PROBE_QUERY.format(nonce="abc")
    assert "[timeout:5]" in query and "[maxsize:1048576]" in query
    for heavy in ("bbox", "area", "around", "way", "rel", "out body", "out geom", "recurse"):
        assert heavy not in query


def test_it_sends_exactly_the_headers_osmnx_sends(interpreter):
    """overpass-api.de answers 406 to the stock python-requests User-Agent
    (measured 2026-08-15), so a probe without our identity reads a healthy
    instance as refusing. Compared against osmnx's own header builder -- the
    one every real walk request goes through -- rather than against our
    constants, so the probe cannot drift from the fetch it speaks for."""
    assert dc.overpass_serving() is True
    headers = interpreter.calls[0]["headers"]
    assert headers == ox._http._get_http_headers()
    assert headers == dc.overpass_headers()
    assert headers["User-Agent"] == dc.OVERPASS_USER_AGENT
    assert "python-requests" not in headers["User-Agent"]
    assert headers["referer"] == dc.OVERPASS_REFERER
    assert headers["Accept-Language"] == dc.OVERPASS_ACCEPT_LANGUAGE


def test_osmnx_is_configured_with_the_same_identity_and_endpoint(monkeypatch):
    """The scheduler cannot import osmnx, so the re-check carries its own copy
    of the endpoint and the identity. Each must equal what osmnx is told, or a
    re-check clears the breaker against a different host, or as a different
    client, than the fetch it is clearing the way for."""
    monkeypatch.delenv(dc.OVERPASS_URL_ENV, raising=False)
    monkeypatch.setattr(ox.settings, "overpass_url", "https://overpass-api.de/api")
    dsn._apply_overpass_url()
    assert dc.DEFAULT_OVERPASS_URL == ox.settings.overpass_url
    assert dc.overpass_url() == ox.settings.overpass_url
    assert ox.settings.http_user_agent == dc.OVERPASS_USER_AGENT
    assert ox.settings.http_referer == dc.OVERPASS_REFERER
    assert ox.settings.http_accept_language == dc.OVERPASS_ACCEPT_LANGUAGE
    # And the mirror override reaches both.
    monkeypatch.setenv(dc.OVERPASS_URL_ENV, "https://overpass.example.org/api")
    dsn._apply_overpass_url()
    assert dc.overpass_url() == ox.settings.overpass_url == "https://overpass.example.org/api"
    assert dsn.OVERPASS_URL_ENV == dc.OVERPASS_URL_ENV


def test_the_mirror_override_and_an_explicit_url_are_where_it_asks(interpreter, monkeypatch):
    monkeypatch.setenv(dc.OVERPASS_URL_ENV, "https://overpass.example.org/api/")
    assert dc.overpass_serving() is True
    assert interpreter.calls[-1]["url"] == "https://overpass.example.org/api/interpreter"
    assert dc.overpass_serving("https://other.example.net/api") is True
    assert interpreter.calls[-1]["url"] == "https://other.example.net/api/interpreter"


def test_the_timeout_reaches_the_request(interpreter):
    """Default and a NON-default value both reach the POST: a hardcoded
    default at the call site would pass a default-only test."""
    assert dc.overpass_serving() is True
    assert interpreter.calls[-1]["timeout"] == dc.OVERPASS_PROBE_TIMEOUT_S == 25.0
    assert dc.overpass_serving(timeout_s=7.5) is True
    assert interpreter.calls[-1]["timeout"] == 7.5


def test_the_timeout_outlasts_the_servers_slot_queue():
    """Overpass holds a request up to 15 s for a free slot before answering
    429, then runs it for up to its declared 5 s. A client timeout inside that
    window would read a queued-then-served probe as a failure."""
    declared = int(re.search(r"\[timeout:(\d+)\]", dc.OVERPASS_PROBE_QUERY).group(1))
    assert dc.OVERPASS_PROBE_TIMEOUT_S > 15 + declared


# ---------------------------------------------------------------------------
# Where it connects: the one IPv4 address osmnx would pin
# ---------------------------------------------------------------------------


def test_it_connects_to_the_ipv4_address_osmnx_would_pin(interpreter):
    """osmnx resolves the host with gethostbyname (one address, IPv4 only)
    before every query and pins the request to it. Plain requests on a
    dual-stack host would take the IPv6 answer -- and Overpass identifies a
    client by its IPv4 address or its IPv6 /64, i.e. possibly a different
    client from the one the walk is refused as."""
    assert dc.overpass_serving() is True
    assert interpreter.lookups == ["overpass-api.de"]
    assert interpreter.calls[0]["connect_to"] == _V4
    # The URL, and so TLS SNI and the Host header, still name the host.
    assert interpreter.calls[0]["url"].startswith("https://overpass-api.de/")


def test_the_pin_matches_osmnx_own_resolver(interpreter, monkeypatch):
    """Cross-check against osmnx itself rather than a restatement of it: run
    osmnx's `_config_dns` under the same fake DNS and compare addresses."""
    assert dc.overpass_serving() is True
    ox._http._config_dns("https://overpass-api.de/api")  # patches socket.getaddrinfo
    osmnx_addr = socket.getaddrinfo("overpass-api.de", 443, 0, socket.SOCK_STREAM)[0][4][0]
    assert interpreter.calls[0]["connect_to"] == osmnx_addr == _V4


def test_the_pin_follows_the_mirror_override(interpreter, monkeypatch):
    monkeypatch.setenv(dc.OVERPASS_URL_ENV, "https://overpass.example.org/api")
    assert dc.overpass_serving() is True
    assert interpreter.lookups == ["overpass.example.org"]
    assert interpreter.calls[0]["connect_to"] == "198.51.100.9"


def test_the_pin_is_scoped_to_the_probe_and_to_the_host(interpreter):
    """The scheduler is a long-lived parent: unlike osmnx's permanent patch,
    this one must be gone afterwards -- even when the request raises -- and
    must never redirect any other host while it is in place."""
    seen_other = []

    def reply(nonce):
        seen_other.append(socket.getaddrinfo("smtp.example.com", 25)[0][4][0])
        return 200, _answer(nonce)

    interpreter.reply = reply
    assert dc.overpass_serving() is True
    assert seen_other == [_V6], "another hostname was redirected during the probe"
    assert socket.getaddrinfo is _dual_stack_getaddrinfo

    def boom(nonce):
        raise requests.exceptions.ConnectionError("[Errno 111] Connection refused")

    interpreter.reply = boom
    assert dc.overpass_serving() is False
    assert socket.getaddrinfo is _dual_stack_getaddrinfo


def test_a_failed_lookup_keeps_it_latched_and_sends_nothing(interpreter, monkeypatch):
    def no_dns(host):
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(socket, "gethostbyname", no_dns)
    assert dc.overpass_serving() is False
    assert interpreter.calls == []
    assert socket.getaddrinfo is _dual_stack_getaddrinfo


# ---------------------------------------------------------------------------
# What counts as serving: an executed probe, and nothing else
# ---------------------------------------------------------------------------


def test_node_one_is_not_required(interpreter):
    """If node 1 is ever deleted the answer has only the probe element; the
    reset test must not start failing closed forever on a data edit."""
    interpreter.reply = lambda nonce: (200, _answer(nonce, with_node=False))
    assert dc.overpass_serving() is True


def test_each_call_asks_with_a_fresh_nonce(interpreter):
    dc.overpass_serving()
    dc.overpass_serving()
    first, second = (_NONCE.search(c["data"]["data"]).group(1) for c in interpreter.calls)
    assert first != second
    assert len(first) >= 12


def test_a_replayed_answer_does_not_clear_it(interpreter):
    """Something that is not an interpreter running THIS query -- a cache, a
    proxy replaying the last good body -- echoes an old nonce."""
    seen = []

    def replay(nonce):
        seen.append(nonce)
        return 200, _answer(seen[0])

    interpreter.reply = replay
    assert dc.overpass_serving() is True  # the first answer really is ours
    assert dc.overpass_serving() is False  # the same body again is not


@pytest.mark.parametrize("code", [403, 406, 429, 500, 502, 503, 504, 509])
def test_every_non_200_keeps_it_latched_even_with_a_perfect_body(interpreter, code):
    """429 = no slot within 15 s; 403 = refused; 406 = our User-Agent; 5xx =
    a front end or load shedding. The pre-flight treats some of these as "can't
    tell -- proceed"; here "can't tell" keeps the breaker latched. The body
    echoes the nonce correctly, so only the status can be what refuses it."""
    interpreter.reply = lambda nonce, c=code: (c, _answer(nonce))
    assert dc.overpass_serving() is False
    assert len(interpreter.calls) == 1, "one request, no retry"


@pytest.mark.parametrize(
    "exc",
    [
        requests.exceptions.ConnectionError("[Errno 111] Connection refused"),
        requests.exceptions.ConnectTimeout("connect timed out"),
        requests.exceptions.ReadTimeout("read timed out"),
        requests.exceptions.SSLError("handshake failure"),
        requests.exceptions.ChunkedEncodingError("connection broken"),
        OSError("network is unreachable"),
        RuntimeError("anything else at all"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_every_transport_failure_keeps_it_latched(interpreter, exc):
    """Connection refused is the 2026-08-14 ban signature. A reset test that
    read it as 'not refusing' would clear the breaker straight into a live ban."""

    def fail(nonce):
        raise exc

    interpreter.reply = fail
    assert dc.overpass_serving() is False
    assert len(interpreter.calls) == 1, "one request, no retry"


_STATUS_BODY = "Connected as: 403941390\nRate limit: 2\n2 slots available now.\n"


@pytest.mark.parametrize(
    "make_body",
    [
        # Overpass reports runtime errors under a 200, in `remark` -- including
        # the "server is probably too busy" dispatcher timeout of 2025-02. Even
        # with the probe echoed, a remark means the query did not run cleanly.
        pytest.param(
            lambda n: _answer(
                n,
                remark="runtime error: open64: 0 Success /osm3s_osm_base "
                "Dispatcher_Client::request_read_and_idx::timeout. "
                "The server is probably too busy to handle your request.",
            ),
            id="runtime-error-remark-with-echo",
        ),
        pytest.param(
            lambda n: _answer(None, remark="runtime error: Query timed out"),
            id="runtime-error-remark",
        ),
        pytest.param(lambda n: _answer(None), id="no-probe-element"),
        pytest.param(lambda n: _answer(None, with_node=False), id="empty-elements"),
        pytest.param(lambda n: _answer("0" * 12), id="wrong-nonce"),
        pytest.param(
            lambda n: json.dumps({"elements": [{"type": "probe", "id": 1}]}), id="probe-no-tags"
        ),
        pytest.param(
            lambda n: json.dumps({"elements": [{"type": "probe", "tags": [n]}]}),
            id="probe-tags-not-an-object",
        ),
        pytest.param(
            lambda n: json.dumps({"elements": [{"type": "node", "tags": {"nonce": n}}]}),
            id="nonce-on-the-wrong-element",
        ),
        pytest.param(lambda n: json.dumps({"elements": {"nonce": n}}), id="elements-not-a-list"),
        pytest.param(lambda n: json.dumps([{"type": "probe", "tags": {"nonce": n}}]), id="a-list"),
        pytest.param(lambda n: json.dumps(None), id="json-null"),
        pytest.param(lambda n: "", id="empty-body"),
        pytest.param(lambda n: "<html><body>maintenance</body></html>", id="html"),
        pytest.param(lambda n: _STATUS_BODY, id="a-status-body"),
        pytest.param(lambda n: _answer(n)[:40], id="truncated-json"),
    ],
)
def test_a_200_that_is_not_an_executed_probe_keeps_it_latched(interpreter, make_body):
    """A captive portal, a maintenance page, a runtime error served with a 200,
    a format change, a /status body: none proves the interpreter ran THIS query."""
    interpreter.reply = lambda nonce: (200, make_body(nonce))
    assert dc.overpass_serving() is False


def test_the_fixture_can_tell_the_two_answers_apart(interpreter):
    """Guards the parametrized cases above against a stand-in that could only
    ever say False: the same fixture, answering the real shape, says True."""
    interpreter.reply = lambda nonce: (200, _answer(nonce))
    assert dc.overpass_serving() is True
    interpreter.reply = lambda nonce: (200, _answer(nonce, remark="runtime error: x"))
    assert dc.overpass_serving() is False


# ---------------------------------------------------------------------------
# At the breaker: the real predicate, on the unchanged budget
# ---------------------------------------------------------------------------


def test_the_breaker_uses_this_predicate_and_its_budget_is_unchanged():
    """#356 changes WHAT a re-check asks, never how often: still one request,
    a 45-min cooldown, at most four a night."""
    assert sched.overpass_serving is dc.overpass_serving
    assert sched.HOST_RECHECK_COOLDOWN_S == 45 * 60
    assert sched.HOST_RECHECKS_PER_NIGHT == 4


def test_a_breaker_driven_by_the_real_probe_clears_only_on_an_executed_query(
    interpreter, monkeypatch
):
    """End to end through HostBreaker with the REAL predicate (conftest pins
    the table to "still refusing"; this test restores the real entry): refused
    re-checks cost one POST each on the cooldown, a clean probe un-latches."""
    monkeypatch.setitem(sched.HOST_RECHECKS, dc.HOST_OVERPASS, dc.overpass_serving)
    now = {"t": 0.0}
    breaker = sched.HostBreaker(clock=lambda: now["t"])
    breaker.trip(dc.HOST_OVERPASS)

    # The 2026-09-21 shape: /status would have said "2 slots available", but
    # the interpreter refuses. The re-check must see the refusal.
    interpreter.reply = lambda nonce: (429, "rate_limited")
    now["t"] = sched.HOST_RECHECK_COOLDOWN_S - 1
    assert breaker.blocking((dc.HOST_OVERPASS,)) == {dc.HOST_OVERPASS}
    assert interpreter.calls == [], "asked before the cooldown"
    now["t"] = sched.HOST_RECHECK_COOLDOWN_S
    assert breaker.blocking((dc.HOST_OVERPASS,)) == {dc.HOST_OVERPASS}
    assert len(interpreter.calls) == 1

    interpreter.reply = lambda nonce: (200, _answer(nonce))
    now["t"] += sched.HOST_RECHECK_COOLDOWN_S
    assert breaker.blocking((dc.HOST_OVERPASS,)) == set()
    assert len(interpreter.calls) == 2
    assert breaker.recoveries[dc.HOST_OVERPASS] == 1
    assert all(c["url"].endswith("/interpreter") for c in interpreter.calls)


def test_a_breaker_driven_by_the_real_probe_spends_at_most_four_queries_a_night(
    interpreter, monkeypatch
):
    """The probe is now METERED, so the cap is what bounds our load on the
    shared instance: four POSTs, however long the night and however many
    launches ask, when the interpreter never serves."""
    monkeypatch.setitem(sched.HOST_RECHECKS, dc.HOST_OVERPASS, dc.overpass_serving)
    interpreter.reply = lambda nonce: (200, _answer(None))  # a 200, but never our probe
    now = {"t": 0.0}
    breaker = sched.HostBreaker(clock=lambda: now["t"])
    breaker.trip(dc.HOST_OVERPASS)
    for _ in range(40):
        now["t"] += sched.HOST_RECHECK_COOLDOWN_S
        assert breaker.blocking((dc.HOST_OVERPASS,)) == {dc.HOST_OVERPASS}
    assert len(interpreter.calls) == sched.HOST_RECHECKS_PER_NIGHT == 4
