"""No-network DNS for the tests that pin how an Overpass probe CONNECTS.

Shared by the breaker's reset test (``test_overpass_reset_probe.py``, issue
#364) and the walk's pre-flight (``test_overpass.py``, issue #366), because both
probes go through ``download_common._pinned_like_osmnx`` and both need the same
dual-stack world to show the pin doing anything.

The world is makelab2's, as its resolver sees overpass-api.de: ``getaddrinfo``
prefers an IPv6 address, ``gethostbyname`` can only return IPv4. Every address
is from a documentation range (RFC 3849 / RFC 5737), so nothing here could
reach a real server.
"""

import socket
import urllib.parse

V6 = "2001:db8::2"
V4 = "192.0.2.52"
# What the fake `gethostbyname` gives any host other than overpass-api.de,
# i.e. a mirror set through OVERPASS_URL.
OTHER_V4 = "198.51.100.9"
OVERPASS_HOST = "overpass-api.de"


def dual_stack_getaddrinfo(host, port, *args, **kwargs):
    """No-network resolver: an IP literal resolves to itself, a name to IPv6."""
    if host in (V4, OTHER_V4):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port))]
    return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (V6, port, 0, 0))]


def recording_gethostbyname(lookups: list):
    """A fake ``socket.gethostbyname`` that appends each host it is asked for."""

    def gethostbyname(host):
        lookups.append(host)
        return V4 if host == OVERPASS_HOST else OTHER_V4

    return gethostbyname


def install(monkeypatch) -> list:
    """Install both fakes; return the list the lookups are recorded into.

    Also replaces osmnx's own ``_http._original_getaddrinfo``, which it
    captured from ``socket`` at IMPORT time: ``_config_dns``'s patch falls
    through to that capture for any host it does not pin, so without this a
    test that calls ``_config_dns`` and then resolves an unpinned name would
    make a real DNS lookup.
    """
    import osmnx._http

    lookups = []
    monkeypatch.setattr(socket, "getaddrinfo", dual_stack_getaddrinfo)
    monkeypatch.setattr(osmnx._http, "_original_getaddrinfo", dual_stack_getaddrinfo)
    monkeypatch.setattr(socket, "gethostbyname", recording_gethostbyname(lookups))
    return lookups


def connect_address(url: str) -> str:
    """The address a request to ``url`` would connect to right now, resolved
    exactly as urllib3 does at connect time -- so a fake request can record it."""
    host = urllib.parse.urlsplit(url).hostname
    return socket.getaddrinfo(host, 443, 0, socket.SOCK_STREAM)[0][4][0]
