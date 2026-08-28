#!/usr/bin/env python3
"""
Measure CARTO's basemap key enforcement and the cost of leaving raster (#284).

Two questions, both answered by a handful of HTTP requests, and both worth
committing because the answers are counter-intuitive enough that the next
person will otherwise re-measure them:

  1. **How does CARTO enforce the key?**  Not with a status code. A keyless
     request, a well-formed but wrong key, and the right key under the wrong
     parameter name all return HTTP 200 ``image/png`` with "API KEY REQUIRED"
     printed across the tile. This script records the status, the byte count
     and the SHA-256 of each variant, so "wrong is byte-identical to absent"
     is a recorded measurement rather than a claim. It also records whether
     the ``Referer`` CARTO collects when issuing the key is enforced.

  2. **What would moving to vector cost?**  Vector needs the same key, so it
     is a rendering-stack decision, not a way off the key. The measurable part
     of that decision is bundle weight: Leaflet cannot draw MVT at all, so
     vector means maplibre-gl. This fetches both libraries from cdnjs with
     ``Accept-Encoding: gzip`` and records what the browser would actually
     transfer.

Writes ``docs/experiments/carto-basemap-key_metrics.json``, which
``docs/experiments/carto-basemap-key.md`` quotes.

**Sends the site's public basemap key to CARTO** -- the same key the deployed
page sends on every tile request -- and nothing else. Makes no calls to any
imagery provider (GSV / Mapillary / KartaView), touches no credentials, and
does not read or write the catalog. About a dozen requests in total.

Usage:
    python scripts/measure_carto_basemap.py
    python scripts/measure_carto_basemap.py --out-dir docs/experiments
    python scripts/measure_carto_basemap.py --dry-run     # print, write nothing
"""

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT_DIR = os.path.join(_PROJECT_ROOT, "docs", "experiments")
EXPERIMENT = "carto-basemap-key"

# The key lives in exactly one place; this reads it rather than carrying a
# second copy (see docs/frontend.md).
BASEMAP_KEY_SOURCE = os.path.join(_PROJECT_ROOT, "www", "js", "streetscape-utils.js")
_CARTO_KEY_RE = re.compile(r'CARTO_BASEMAP_KEY\s*=\s*"([^"]+)"')

# One stable tile with real content on it (San Francisco, z12). An all-water
# tile would be near-identical with and without a watermark, which would make
# the byte comparison meaningless.
TILE = "https://a.basemaps.cartocdn.com/dark_all/12/655/1583.png"

# The domain the key was issued against, plus a domain it was not. If the two
# return the same bytes, the key is bearer-style rather than origin-locked.
ISSUED_ORIGIN = "https://makeabilitylab.cs.washington.edu/"
FOREIGN_ORIGIN = "https://example.invalid/"

# The two renderers the raster-vs-vector decision is actually between. Both
# halves (script + stylesheet) count: a map needs each to draw.
BUNDLES = {
    "leaflet": [
        "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js",
        "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.css",
    ],
    "maplibre-gl": [
        "https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/5.24.0/maplibre-gl.js",
        "https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/5.24.0/maplibre-gl.css",
    ],
}

TIMEOUT_S = 60
USER_AGENT = "streetscape-tracker-measurement"


def carto_basemap_key(source_path: str = BASEMAP_KEY_SOURCE) -> str:
    """Read the site's CARTO key out of the frontend module that owns it."""
    with open(source_path, encoding="utf-8") as f:
        match = _CARTO_KEY_RE.search(f.read())
    if not match:
        raise ValueError(f"no CARTO_BASEMAP_KEY const found in {source_path}")
    return match.group(1)


def fetch(url: str, headers: dict | None = None) -> dict:
    """GET a URL and describe the response by size and digest, never by content.

    The digest is the whole point: the watermark is inside a valid PNG, so
    "did this request work?" can only be answered by comparing bytes against
    a request we know did not.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            body = response.read()
            status = response.status
            content_type = response.headers.get("Content-Type")
            encoding = response.headers.get("Content-Encoding")
    except urllib.error.HTTPError as exc:
        body, status = exc.read(), exc.code
        content_type = exc.headers.get("Content-Type")
        encoding = exc.headers.get("Content-Encoding")
    return {
        "status": status,
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "content_type": content_type,
        "content_encoding": encoding,
    }


def measure_key_enforcement(key: str) -> dict:
    """How CARTO answers each way of getting the key right and wrong."""
    bogus = (
        re.sub(r"[0-9a-f]{8}$", "deadbeef", key)
        if re.search(r"[0-9a-f]{8}$", key)
        else key[:-8] + "deadbeef"
    )
    variants = {
        "keyed": f"{TILE}?key={key}",
        "keyless": TILE,
        "wrong_key_value": f"{TILE}?key={bogus}",
        "wrong_param_name": f"{TILE}?api_key={key}",
    }
    results = {name: fetch(url) for name, url in variants.items()}

    keyed_digest = results["keyed"]["sha256"]
    for _name, result in results.items():
        result["differs_from_keyed"] = result["sha256"] != keyed_digest
    return results


def measure_referer_enforcement(key: str) -> dict:
    """Whether the domain CARTO collects at issue time is actually enforced."""
    url = f"{TILE}?key={key}"
    results = {
        "issued_origin": fetch(url, {"Referer": ISSUED_ORIGIN}),
        "foreign_origin": fetch(url, {"Referer": FOREIGN_ORIGIN}),
        "no_referer": fetch(url),
    }
    baseline = results["issued_origin"]["sha256"]
    for result in results.values():
        result["matches_issued_origin"] = result["sha256"] == baseline
    return results


def measure_bundles() -> dict:
    """What each renderer costs the browser, gzipped, as a CDN would serve it."""
    out = {}
    for library, urls in BUNDLES.items():
        assets = {}
        for url in urls:
            result = fetch(url, {"Accept-Encoding": "gzip"})
            result["url"] = url
            assets[os.path.basename(url)] = result
        out[library] = {
            "assets": assets,
            "total_gzipped_bytes": sum(a["bytes"] for a in assets.values()),
        }
    return out


def build_metrics(key: str) -> dict:
    key_enforcement = measure_key_enforcement(key)
    referer = measure_referer_enforcement(key)
    bundles = measure_bundles()

    leaflet = bundles["leaflet"]["total_gzipped_bytes"]
    maplibre = bundles["maplibre-gl"]["total_gzipped_bytes"]

    return {
        "_about": {
            "experiment": EXPERIMENT,
            "writeup": f"docs/experiments/{EXPERIMENT}.md",
            "generated_by": "scripts/measure_carto_basemap.py --out-dir docs/experiments",
            "note": (
                "CARTO began requiring an API key on basemaps.cartocdn.com (2026-08-28) and "
                "enforces it by watermarking the tile, not by returning an error, so every way "
                "of getting the key wrong is HTTP 200. `key_enforcement` compares each wrong "
                "variant against the keyed request BY DIGEST because a status code cannot tell "
                "them apart. `referer_enforcement` tests whether the domain CARTO collects when "
                "issuing a key is enforced. `renderer_bundles` is gzipped transfer size off "
                "cdnjs, sizing the raster-vs-vector decision -- vector needs the same key, so "
                "the only thing switching buys or costs is the rendering stack. The key itself "
                "is not recorded here; it lives in www/js/streetscape-utils.js."
            ),
        },
        "tile": TILE,
        "key_enforcement": key_enforcement,
        "referer_enforcement": referer,
        "renderer_bundles": bundles,
        "summary": {
            "wrong_is_indistinguishable_from_absent": (
                key_enforcement["keyless"]["sha256"]
                == key_enforcement["wrong_key_value"]["sha256"]
                == key_enforcement["wrong_param_name"]["sha256"]
            ),
            "keyed_differs_from_keyless": key_enforcement["keyless"]["differs_from_keyed"],
            "every_failure_mode_returns_200": all(
                r["status"] == 200 for r in key_enforcement.values()
            ),
            "referer_enforced": not (
                referer["foreign_origin"]["matches_issued_origin"]
                and referer["no_referer"]["matches_issued_origin"]
            ),
            "leaflet_gzipped_bytes": leaflet,
            "maplibre_gl_gzipped_bytes": maplibre,
            "maplibre_over_leaflet_ratio": round(maplibre / leaflet, 2) if leaflet else None,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--dry-run", action="store_true", help="print the metrics, write nothing")
    args = parser.parse_args()

    metrics = build_metrics(carto_basemap_key())
    rendered = json.dumps(metrics, indent=2) + "\n"

    if args.dry_run:
        print(rendered, end="")
        return 0

    out_path = os.path.join(args.out_dir, f"{EXPERIMENT}_metrics.json")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(rendered)
    print(f"wrote {out_path}")

    s = metrics["summary"]
    print(
        f"  wrong key is byte-identical to no key : {s['wrong_is_indistinguishable_from_absent']}"
    )
    print(f"  every failure mode returns HTTP 200   : {s['every_failure_mode_returns_200']}")
    print(f"  Referer enforced                      : {s['referer_enforced']}")
    print(
        f"  maplibre-gl vs leaflet (gzipped)      : "
        f"{s['maplibre_gl_gzipped_bytes']:,} B vs {s['leaflet_gzipped_bytes']:,} B "
        f"({s['maplibre_over_leaflet_ratio']}x)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
