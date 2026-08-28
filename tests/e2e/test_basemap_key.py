"""Live check that the CARTO basemap key is still buying us unwatermarked tiles.

CARTO began requiring an API key on ``basemaps.cartocdn.com`` (2026-08-28), and
the failure mode is silent by construction: a request with **no key, a bogus
key, a mangled key, or the right key under the wrong parameter name** all
return HTTP 200, ``image/png``, and a tile with "API KEY REQUIRED" printed
across it. Nothing in the frontend's fetch handling can see that -- it is a
valid image -- so the site rendered watermarked for an unknown period and the
break presented as a styling oddity rather than an outage.

That makes this the one thing worth checking over the network, because it is
the one thing the offline suite structurally cannot:
``www/js/__tests__/streetscape-utils.test.js`` pins the SHAPE of the request we
build (the ``key=`` parameter, the key that reaches CARTO, the attribution) and
that neither page script builds a tile URL of its own, but no offline test can
know whether CARTO still honours the key.

The assertion is differential rather than a pinned hash: fetch one tile with
the key and the same tile without it, and require the bytes to differ. That
holds without hardcoding what a watermark looks like, and it goes red for every
way the key can stop working --

  * revoked, expired, or rotated out from under us,
  * the 5M-request/month free ceiling exhausted (including by someone who
    scraped the key out of this public repo -- CARTO does not enforce the
    domain it collected, so the key is bearer-style),
  * a future edit that drops or misspells the query parameter.

It would also go red if CARTO stopped watermarking keyless requests entirely.
That is a false alarm, but a useful one: it means the constraint this whole
mechanism exists for has changed and is worth re-reading.

Marked ``e2e`` so it stays out of the fast, no-network suite and runs in the
non-blocking e2e CI job. It needs no browser.
"""

import os
import re
import time
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.e2e

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
_UTILS_JS = os.path.join(_PROJECT_ROOT, "www", "js", "streetscape-utils.js")

# One arbitrary but stable tile (San Francisco, z12). Any tile with content
# works; an all-water tile would compress to near-identical bytes with and
# without a watermark and would make the differential meaningless.
_TILE = "https://a.basemaps.cartocdn.com/dark_all/12/655/1583.png"

_TIMEOUT_S = 30

# CARTO occasionally answers a burst of requests with a 429/5xx. That is a
# statement about the moment, not about our key, so retry it once rather than
# spend a red run on it -- this job already carries two known flakes and a
# third would just train people to ignore it. A 4xx other than 429 is a real
# verdict on the request and is NOT retried.
_TRANSIENT_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
_ATTEMPTS = 2


def _carto_basemap_key() -> str:
    """The key the site actually ships, read from its single home."""
    with open(_UTILS_JS, encoding="utf-8") as f:
        match = re.search(r'CARTO_BASEMAP_KEY\s*=\s*"([^"]+)"', f.read())
    assert match, f"no CARTO_BASEMAP_KEY const in {_UTILS_JS}"
    return match.group(1)


def _fetch(url: str) -> tuple[int, bytes]:
    """GET a tile, turning a network-level failure into a skip, not a failure.

    A DNS or connection error says something about the machine running the
    test; it says nothing about our key, and failing on it would train people
    to ignore this test. An HTTP error is different -- that IS a response from
    CARTO -- so it is returned for the assertions to judge.
    """
    request = urllib.request.Request(url, headers={"User-Agent": "streetscape-tracker-e2e"})
    for attempt in range(_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:  # a real answer from CARTO
            status, body = exc.code, exc.read()
            if status not in _TRANSIENT_STATUSES or attempt == _ATTEMPTS - 1:
                return status, body
        except (urllib.error.URLError, TimeoutError) as exc:
            pytest.skip(f"cannot reach {_TILE}: {exc}")
        time.sleep(2)
    raise AssertionError("unreachable")


def test_the_carto_key_still_buys_an_unwatermarked_tile():
    keyed_status, keyed = _fetch(f"{_TILE}?key={_carto_basemap_key()}")
    keyless_status, keyless = _fetch(_TILE)

    assert keyed_status == 200, f"keyed tile request returned HTTP {keyed_status}"
    assert keyless_status == 200, (
        f"keyless tile returned HTTP {keyless_status}; CARTO has always answered 200 here, "
        f"so the differential below is no longer measuring what it was written to measure"
    )
    assert keyed, "keyed tile came back empty"

    assert keyed != keyless, (
        "the keyed tile is byte-identical to the keyless one, so the key is buying nothing "
        "and every map on the site is rendering CARTO's 'API KEY REQUIRED' watermark. "
        "Check the key in www/js/streetscape-utils.js against the CARTO account: revoked, "
        "rotated, or the 5M/month free ceiling exhausted. This never surfaces as an error "
        "in the browser -- the watermark is inside a valid PNG."
    )
