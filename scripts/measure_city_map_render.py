#!/usr/bin/env python3
"""
Measure the city map's dot tier before and after the viewport-true change (#77).

Serves one or more copies of ``www/`` from a local HTTP server, opens a dense
synthetic city in headless Chromium (Playwright), and records, per page copy and
per city size:

  * load: navigation to the capture-date chart existing, and the JS heap after a
    forced GC (Chrome DevTools Protocol ``Performance.getMetrics``);
  * the pano index the page builds (the ``pano-index`` performance measure and
    ``indexByteLength``), where the copy has one;
  * three kinds of move, each timed to the end of the synchronous handler and to
    the second animation frame after it: zoom +2 from the fitted view, a jump to
    street level (zoom 16) at the city's centre, and a run of 300 px pans there;
  * after every move, the markers on the map, the markers inside the visible
    map, and (where the copy can say) how many panoramas the view truly holds.

The cities come from ``tests/e2e/build_fixture.write_dense_city``: a 100 m
street lattice with a panorama every 10 m, so density is constant and a larger
city is only a wider one. That is synthetic in the way that matters least for
the dot tier (a street-level view holds about the same number of panoramas at
every size) and most for load (a real city's CSV has more columns and its
streets are not a lattice).

**Offline except for the page's own libraries.** The page's scripts load from
cdnjs exactly as in the e2e suite; CARTO basemap tiles and analytics are
aborted. No imagery provider, Overpass or Nominatim is contacted, and no
catalog is read.

Usage:
    python scripts/measure_city_map_render.py \\
        --www before=/path/to/main/www --www after=www \\
        --sizes 60000,200000,1000000 --repeats 3 --work-dir /tmp/dense \\
        --out docs/experiments/city-map-viewport-dots_metrics.json
"""

import argparse
import functools
import http.server
import json
import os
import platform
import socketserver
import subprocess
import sys
import threading
import time

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(0, os.path.join(_PROJECT_ROOT, "scripts"))

from experiment_stats import describe  # noqa: E402

VIEWPORT = {"width": 1440, "height": 900}
STREET_ZOOM = 16
N_PANS = 6
PAN_PX = 300

_STATE_JS = """() => {
  const b = map.getBounds();
  let inView = 0;
  for (const m of onMap) if (b.contains(m.getLatLng())) inView++;
  const plan = typeof lastPlan !== "undefined" ? lastPlan : null;
  return {
    zoom: map.getZoom(),
    on_map: onMap.size,
    drawn_in_view: inView,
    true_in_view: plan && plan.visible != null ? plan.visible : null,
    tier: typeof mapTier !== "undefined" ? mapTier : null,
  };
}"""

# Timed in the page: the synchronous part (Leaflet fires moveend inside a
# non-animated setView), then two animation frames, by which time the
# renderer has painted what the handler added.
_MOVE_JS = """async (move) => {
  const t0 = performance.now();
  if (move.kind === "zoom") map.setZoom(map.getZoom() + move.dz, { animate: false });
  else if (move.kind === "view") map.setView(move.center, move.zoom, { animate: false });
  else map.panBy([move.px, 0], { animate: false });
  const t1 = performance.now();
  await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  return { sync_ms: t1 - t0, paint_ms: performance.now() - t0 };
}"""

_INDEX_JS = """() => {
  const m = performance.getEntriesByName("pano-index")[0];
  const idx = typeof panoIndex !== "undefined" ? panoIndex : null;
  return {
    index_ms: m ? m.duration : null,
    index_level: idx ? idx.level : null,
    index_cells: idx ? idx.cellCount : null,
    index_bytes: idx && typeof indexByteLength === "function" ? indexByteLength(idx) : null,
  };
}"""


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def _serve(directory):
    httpd = _Server(("127.0.0.1", 0), functools.partial(_QuietHandler, directory=directory))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _git(*args):
    try:
        return subprocess.run(
            ["git", *args], cwd=_PROJECT_ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _portable(path):
    """A path as the committed record may hold it: repo-relative, or a placeholder.

    A copy outside the repo (e.g. ``git archive origin/main www`` unpacked into a
    scratch dir) is recorded as ``<outside-repo>``, never by a machine's path.
    """
    if os.path.commonpath([path, _PROJECT_ROOT]) == _PROJECT_ROOT:
        return os.path.relpath(path, _PROJECT_ROOT)
    return "<outside-repo>"


def _heap_bytes(cdp):
    cdp.send("HeapProfiler.collectGarbage")
    metrics = cdp.send("Performance.getMetrics")["metrics"]
    return next(m["value"] for m in metrics if m["name"] == "JSHeapUsedSize")


def _one_load(browser, base, data_dir, city):
    """Open the city once and run every move; returns one repeat's record."""
    context = browser.new_context(viewport=VIEWPORT)
    page = context.new_page()

    def data(route):
        name = route.request.url.split("/data/")[-1].split("?")[0]
        path = os.path.join(data_dir, name)
        if not os.path.isfile(path):
            return route.fulfill(status=404, body=b"")
        with open(path, "rb") as f:
            body = f.read()
        route.fulfill(
            status=200,
            body=body,
            headers={
                "Content-Type": "application/octet-stream",
                "Access-Control-Allow-Origin": "*",
            },
        )

    page.route("**/streetscape-tracker/data/**", data)
    page.route("**/*basemaps.cartocdn.com/**", lambda r: r.abort())
    page.route("**/*googletagmanager.com/**", lambda r: r.abort())
    page.route("**/*google-analytics.com/**", lambda r: r.abort())
    cdp = context.new_cdp_session(page)
    cdp.send("Performance.enable")

    page.goto(f"{base}/city.html?file={city['csv']}")
    page.wait_for_function(
        "() => typeof Chart !== 'undefined' && Chart.getChart(document.getElementById('temporal-plot'))",
        polling="raf",
        timeout=600_000,
    )
    load_ms = page.evaluate("performance.now()")
    # main's fitBounds animates (0.25 s); let any move it started finish.
    page.wait_for_timeout(600)
    record = {
        "load_ms": load_ms,
        "heap_bytes": _heap_bytes(cdp),
        "fitted": page.evaluate(_STATE_JS),
    }
    record.update(page.evaluate(_INDEX_JS))

    s, w, n, e = city["bounds"]
    moves = [("zoom_plus_2", {"kind": "zoom", "dz": 2})]
    moves.append(
        ("street_view", {"kind": "view", "center": [(s + n) / 2, (w + e) / 2], "zoom": STREET_ZOOM})
    )
    moves += [(f"pan_{i}", {"kind": "pan", "px": PAN_PX}) for i in range(N_PANS)]
    record["moves"] = []
    for name, move in moves:
        timing = page.evaluate(_MOVE_JS, move)
        record["moves"].append({"move": name, **timing, **page.evaluate(_STATE_JS)})
    record["heap_bytes_after_moves"] = _heap_bytes(cdp)
    context.close()
    return record


def _summarize(repeats):
    pans = [m for r in repeats for m in r["moves"] if m["move"].startswith("pan_")]

    def moves_named(name):
        return [m for r in repeats for m in r["moves"] if m["move"] == name]

    out = {
        "load_ms": describe([r["load_ms"] for r in repeats], 1),
        "heap_mb": describe([r["heap_bytes"] / 2**20 for r in repeats], 1),
    }
    if repeats[0]["index_ms"] is not None:
        out["index_ms"] = describe([r["index_ms"] for r in repeats], 2)
    for name in ("zoom_plus_2", "street_view"):
        ms = moves_named(name)
        out[name] = {
            "sync_ms": describe([m["sync_ms"] for m in ms], 2),
            "paint_ms": describe([m["paint_ms"] for m in ms], 2),
        }
    out["pan"] = {
        "sync_ms": describe([m["sync_ms"] for m in pans], 2),
        "paint_ms": describe([m["paint_ms"] for m in pans], 2),
        "drawn_in_view": describe([m["drawn_in_view"] for m in pans], 0),
    }
    true_counts = [m["true_in_view"] for m in pans if m["true_in_view"] is not None]
    if true_counts:
        out["pan"]["true_in_view"] = describe(true_counts, 0)
    return out


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--www", action="append", required=True, metavar="LABEL=DIR")
    ap.add_argument("--sizes", default="60000,200000,1000000")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--work-dir", required=True, help="where the dense cities are written")
    ap.add_argument("--out", help="JSON output (default: stdout)")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    from tests.e2e.build_fixture import write_dense_city

    copies = []
    for spec in args.www:
        label, _, directory = spec.partition("=")
        copies.append((label, os.path.abspath(directory)))
    sizes = [int(x) for x in args.sizes.split(",")]

    cities = {}
    for n in sizes:
        out_dir = os.path.join(args.work_dir, str(n))
        os.makedirs(out_dir, exist_ok=True)
        t0 = time.perf_counter()
        cities[n] = write_dense_city(out_dir, n_panos=n)
        cities[n]["write_s"] = round(time.perf_counter() - t0, 1)
        cities[n]["csv_bytes"] = os.path.getsize(os.path.join(out_dir, cities[n]["csv"]))
        print(f"wrote {n:,} panos in {cities[n]['write_s']} s", file=sys.stderr, flush=True)

    www_args = " ".join(f"--www {label}={_portable(d)}" for label, d in copies)
    out_arg = f" --out {_portable(os.path.abspath(args.out))}" if args.out else ""
    doc = {
        "generated_by": (
            f"python scripts/measure_city_map_render.py {www_args} --sizes {args.sizes}"
            f" --repeats {args.repeats} --work-dir {_portable(os.path.abspath(args.work_dir))}"
            f"{out_arg}"
        ),
        "commit": _git("rev-parse", "HEAD"),
        "main_commit": _git("rev-parse", "origin/main"),
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        "viewport": VIEWPORT,
        "street_zoom": STREET_ZOOM,
        "pans_per_load": N_PANS,
        "pan_px": PAN_PX,
        "copies": [{"label": label, "www": _portable(d)} for label, d in copies],
        "results": [],
    }
    with sync_playwright() as p:
        browser = p.chromium.launch()
        doc["chromium"] = browser.version
        for label, directory in copies:
            httpd, base = _serve(directory)
            try:
                for n in sizes:
                    city = cities[n]
                    repeats = []
                    for k in range(args.repeats):
                        repeats.append(
                            _one_load(browser, base, os.path.join(args.work_dir, str(n)), city)
                        )
                        print(
                            f"{label} {n:,} #{k + 1}: {repeats[-1]['load_ms']:.0f} ms",
                            file=sys.stderr,
                            flush=True,
                        )
                    doc["results"].append(
                        {
                            "copy": label,
                            "n_panos": city["n_panos"],
                            "csv_bytes": city["csv_bytes"],
                            "summary": _summarize(repeats),
                            "repeats": repeats,
                        }
                    )
            finally:
                httpd.shutdown()
        browser.close()

    text = json.dumps(doc, indent=1) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
