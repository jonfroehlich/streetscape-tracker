#!/usr/bin/env python3
"""
Why does the GSV grid run fall short of its configured rate? (issue #304)

Offline, no credentials, never contacts Google. Drives the REAL
``download_gsv.collect_points_async`` against a local HTTPS stand-in for the
Street View metadata endpoint, behind a proxy that delays every TCP chunk so
connect and the TLS handshake pay a real round trip, and measures the rate the
engine actually sustains.

    python scripts/gsv_throughput_bench.py --baseline-ref 2a94a68 \
        --reps 3 --docs-dir docs/experiments

writes ``docs/experiments/gsv-throughput_metrics.json`` (committed) and the raw
per-run records to ``experiments/gsv-throughput/raw.jsonl`` (gitignored).

WHAT IS AND IS NOT MEASURED
---------------------------
The stand-in answers OK or ZERO_RESULTS by a hash of the location (so the
coverage fraction is exact and the same points get the same answers in every
arm), with a lognormal response latency whose mean differs per status. That
latency is an INPUT, not a measurement of Google: nothing here can observe how
fast Google answers. The scenarios bracket it, and the ``fit`` scenario is the
one whose baseline curve most resembles production's ``runs`` table -- a fit,
labelled as one in the writeup, never a measurement.

What IS measured, per arm: requests per minute over the steady state (server
arrivals after the first ``--warmup-s`` seconds, so the limiter's one-second
start burst is excluded), the client process's CPU seconds per request, the
number of TCP connections the client opened, and peak RSS.

ARMS
----
baseline      the engine at ``--baseline-ref`` (a fresh connector per batch, a
              barrier after every batch, the zeroing token bucket), extracted
              with ``git archive`` into the gitignored experiments dir.
depth{N}      this checkout's engine with ``download_gsv.PIPELINE_DEPTH = N``.
              ``depth1`` is connection reuse alone (the barrier kept).
depth4-oldpacer  this checkout's engine at depth 4 with the token bucket's
              pre-#304 ``acquire`` patched back in, to attribute the pacer fix.

Every arm runs ``--reps`` times per (scenario, coverage) cell, interleaved, and
the JSON reports each cell's median and range.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import random
import resource
import socket
import ssl
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RAW_DIR = REPO / "experiments" / "gsv-throughput"

# (name, zero_results mean ms, ok mean ms). Lognormal sigma 0.5 for both.
SCENARIOS = {
    "fit": (15.0, 45.0),
    "slow-ok": (30.0, 60.0),
    "slower-ok": (30.0, 90.0),
}
COVERAGES = (0.03, 0.25, 0.5, 0.75)
ARMS = ("baseline", "depth1", "depth2", "depth4", "depth4-oldpacer")


# ---------------------------------------------------------------- the server


def _serve(args: argparse.Namespace) -> None:
    """Fake metadata endpoint. GET /stats returns arrival timestamps."""
    from aiohttp import web

    arrivals: list[float] = []

    def draw(mean_ms: float) -> float:
        if mean_ms <= 0:
            return 0.0
        mu = math.log(mean_ms) - args.sigma**2 / 2
        return random.lognormvariate(mu, args.sigma) / 1000

    async def meta(request):
        arrivals.append(time.monotonic())
        loc = request.query["location"]
        digest = hashlib.md5(loc.encode()).hexdigest()
        lat, lon = map(float, loc.split(","))
        if int(digest[:8], 16) / 0xFFFFFFFF < args.cov:
            await asyncio.sleep(draw(args.ok_ms))
            body = {
                "copyright": "© 2024 Google",
                "date": "2023-07",
                "location": {"lat": lat + 1.25e-5, "lng": lon - 7.5e-6},
                "pano_id": digest[:22],
                "status": "OK",
            }
        else:
            await asyncio.sleep(draw(args.zero_ms))
            body = {"status": "ZERO_RESULTS"}
        # indent=3 mimics Google's pretty-printed body size.
        return web.Response(text=json.dumps(body, indent=3), content_type="application/json")

    async def stats(_request):
        return web.json_response({"arrivals": arrivals})

    async def reset(_request):
        arrivals.clear()
        return web.json_response({})

    app = web.Application()
    app.router.add_get("/maps/api/streetview/metadata", meta)
    app.router.add_get("/stats", stats)
    app.router.add_get("/reset", reset)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(args.cert, args.key)

    async def main():
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", args.port, ssl_context=ctx, backlog=2048).start()
        print("ready", flush=True)
        await asyncio.Event().wait()

    asyncio.run(main())


# ----------------------------------------------------------------- the proxy


def _proxy(args: argparse.Namespace) -> None:
    """Forward TCP to --target, delaying every chunk by --delay-ms each way."""
    delay = args.delay_ms / 1000

    async def pipe(reader, writer):
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        async def sender():
            while True:
                due, data = await queue.get()
                if due > loop.time():
                    await asyncio.sleep(due - loop.time())
                if data is None:
                    writer.close()
                    return
                writer.write(data)

        task = asyncio.create_task(sender())
        try:
            while data := await reader.read(65536):
                queue.put_nowait((loop.time() + delay, data))
        except OSError:
            pass
        queue.put_nowait((loop.time() + delay, None))
        await task

    async def handle(client_reader, client_writer):
        await asyncio.sleep(2 * delay)  # the SYN / SYN-ACK round trip
        try:
            server_reader, server_writer = await asyncio.open_connection("127.0.0.1", args.target)
        except OSError:
            client_writer.close()
            return
        await asyncio.gather(pipe(client_reader, server_writer), pipe(server_reader, client_writer))

    async def main():
        server = await asyncio.start_server(handle, "127.0.0.1", args.listen, backlog=2048)
        print("ready", flush=True)
        async with server:
            await server.serve_forever()

    asyncio.run(main())


# ---------------------------------------------------------------- the client


def _old_acquire(self) -> object:
    """``AsyncRateLimiter.acquire``'s token-bucket path as of 2a94a68."""

    async def acquire():
        if not self._enabled:
            return
        async with self._lock:
            now = self._now()
            if self._last_refill is None:
                self._last_refill = now
            self._tokens = min(
                self._capacity, self._tokens + (now - self._last_refill) * self._rate
            )
            self._last_refill = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            await asyncio.sleep((1.0 - self._tokens) / self._rate)
            self._last_refill = self._now()
            self._tokens = 0.0

    return acquire()


def _client(args: argparse.Namespace) -> None:
    """Run one arm in THIS process and print one JSON record."""
    sys.path.insert(0, args.engine_root)
    import aiohttp
    from aiohttp.abc import AbstractResolver

    connects = [0]

    class LocalResolver(AbstractResolver):
        async def resolve(self, host, port=0, family=0):
            return [
                {
                    "hostname": host,
                    "host": "127.0.0.1",
                    "port": args.port,
                    "family": socket.AF_INET,
                    "proto": 0,
                    "flags": 0,
                }
            ]

        async def close(self):
            pass

    class LocalConnector(aiohttp.TCPConnector):
        """Sends maps.googleapis.com to the proxy; TLS still negotiated, unverified."""

        def __init__(self, *a, **kw):
            kw.setdefault("ssl", False)
            kw.setdefault("resolver", LocalResolver())
            super().__init__(*a, **kw)

        async def _create_direct_connection(self, *a, **kw):
            connects[0] += 1
            return await super()._create_direct_connection(*a, **kw)

    aiohttp.TCPConnector = LocalConnector

    from streetscape_metadata_tracker import download_common, download_gsv

    assert Path(download_gsv.__file__).resolve().is_relative_to(Path(args.engine_root).resolve())
    if args.depth:
        download_gsv.PIPELINE_DEPTH = args.depth
    if args.old_pacer:
        download_common.AsyncRateLimiter.acquire = _old_acquire

    import logging

    logging.basicConfig(level=logging.ERROR)
    side = int(math.isqrt(args.n)) + 1
    points = [
        (47.6 + (k // side) * 1.8e-4, -122.3 + (k % side) * 2.7e-4, k // side, k % side)
        for k in range(args.n)
    ]
    ctx = ssl._create_unverified_context()

    def stats(path):
        import urllib.request

        url = f"https://127.0.0.1:{args.server_port}/{path}"
        return json.loads(urllib.request.urlopen(url, context=ctx).read())

    stats("reset")
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "bench_width_1_height_1_step_20_2026-09-30.csv.gz")
        wall0, cpu0 = time.perf_counter(), time.process_time()
        result = asyncio.run(
            download_gsv.collect_points_async(
                points,
                "BENCHKEY",
                out,
                batch_size=args.batch_size,
                connection_limit=args.connection_limit,
                max_requests_per_minute=args.rate,
            )
        )
        wall, cpu = time.perf_counter() - wall0, time.process_time() - cpu0
        ok_rows = int((result["df"]["status"] == "OK").sum())
    arrivals = sorted(stats("stats")["arrivals"])
    t0 = arrivals[0] + args.warmup_s
    steady = [t for t in arrivals if t >= t0]
    steady_rate = (len(steady) - 1) / (steady[-1] - steady[0]) * 60 if len(steady) > 1 else None
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(
        json.dumps(
            {
                "requests": result["api_requests"],
                "ok_rows": ok_rows,
                "wall_s": round(wall, 3),
                "whole_run_req_per_min": round(result["api_requests"] / wall * 60),
                "steady_req_per_min": round(steady_rate) if steady_rate else None,
                "cpu_ms_per_request": round(cpu / result["api_requests"] * 1000, 4),
                "client_connects": connects[0],
                # ru_maxrss is bytes on macOS, KiB on Linux.
                "peak_rss_mb": round(rss / (1e6 if sys.platform == "darwin" else 1e3), 1),
            }
        ),
        flush=True,
    )


# ----------------------------------------------------------------- the sweep


def _start(cmd: list[str]) -> subprocess.Popen:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
    if proc.stdout.readline().strip() != "ready":
        raise RuntimeError(f"did not start: {cmd}")
    return proc


def _extract_baseline(ref: str) -> Path:
    dest = RAW_DIR / f"engine-{ref}"
    if not (dest / "streetscape_metadata_tracker").exists():
        dest.mkdir(parents=True, exist_ok=True)
        archive = subprocess.run(
            ["git", "-C", str(REPO), "archive", ref, "streetscape_metadata_tracker"],
            check=True,
            capture_output=True,
        ).stdout
        subprocess.run(["tar", "-x", "-C", str(dest)], input=archive, check=True)
    return dest


def _summarize(records: list[dict], key: str) -> dict | None:
    values = [r[key] for r in records if r.get(key) is not None]
    if not values:  # e.g. a cpu-ceiling run shorter than the warmup window
        return None
    return {
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
        "n": len(values),
    }


def _sweep(args: argparse.Namespace) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    cert, key = RAW_DIR / "cert.pem", RAW_DIR / "key.pem"
    if not cert.exists():
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key)]
            + ["-out", str(cert), "-days", "30", "-subj", "/CN=maps.googleapis.com"],
            check=True,
            capture_output=True,
        )
    baseline_root = _extract_baseline(args.baseline_ref)
    head = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    raw_path = RAW_DIR / "raw.jsonl"
    me = [sys.executable, str(Path(__file__).resolve())]
    records: list[dict] = []
    port = args.port0

    def client(arm: str, proxy_port: int, server_port: int, rate: int, n: int) -> dict:
        cmd = me + ["client", "--port", str(proxy_port), "--server-port", str(server_port)]
        cmd += ["--n", str(n), "--rate", str(rate), "--warmup-s", str(args.warmup_s)]
        if arm == "baseline":
            cmd += ["--engine-root", str(baseline_root)]
        else:
            cmd += ["--engine-root", str(REPO), "--depth", arm.split("-")[0].removeprefix("depth")]
            if arm.endswith("oldpacer"):
                cmd += ["--old-pacer"]
        out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
        return json.loads(out.strip().splitlines()[-1])

    cells = [(s, c) for s in args.scenarios for c in args.coverages]
    if args.cpu_ceiling:
        cells = [("cpu-ceiling", 0.5)] + cells
    for scenario, cov in cells:
        zero_ms, ok_ms = SCENARIOS.get(scenario, (0.0, 0.0))
        port += 2
        server = _start(
            me
            + ["serve", "--port", str(port), "--cov", str(cov), "--cert", str(cert)]
            + ["--key", str(key), "--ok-ms", str(ok_ms), "--zero-ms", str(zero_ms)]
        )
        proxy = None
        proxy_port = port
        if scenario != "cpu-ceiling":
            proxy_port = port + 1
            proxy = _start(
                me
                + ["proxy", "--listen", str(proxy_port), "--target", str(port)]
                + ["--delay-ms", str(args.rtt_ms / 2)]
            )
        # cpu-ceiling: no latency, no proxy, pacing off -- what one core can do.
        rate, arms = (0, ("baseline", "depth4")) if scenario == "cpu-ceiling" else (48000, ARMS)
        try:
            for rep in range(args.reps):
                for arm in arms:  # interleaved, so drift hits every arm alike
                    rec = client(arm, proxy_port, port, rate, args.n)
                    rec.update(scenario=scenario, coverage=cov, arm=arm, rep=rep)
                    rec.update(zero_ms=zero_ms, ok_ms=ok_ms, rtt_ms=args.rtt_ms)
                    records.append(rec)
                    with open(raw_path, "a") as f:
                        f.write(json.dumps(rec) + "\n")
                    print(json.dumps(rec), flush=True)
        finally:
            for proc in (server, proxy):
                if proc is not None:
                    proc.terminate()
                    proc.wait()

    cells_out = []
    for scenario, cov in cells:
        for arm in ARMS:
            recs = [
                r
                for r in records
                if r["scenario"] == scenario and r["coverage"] == cov and r["arm"] == arm
            ]
            if not recs:
                continue
            cells_out.append(
                {
                    "scenario": scenario,
                    "coverage": cov,
                    "arm": arm,
                    "zero_ms": recs[0]["zero_ms"],
                    "ok_ms": recs[0]["ok_ms"],
                    "steady_req_per_min": _summarize(recs, "steady_req_per_min"),
                    "whole_run_req_per_min": _summarize(recs, "whole_run_req_per_min"),
                    "cpu_ms_per_request": _summarize(recs, "cpu_ms_per_request"),
                    "client_connects": _summarize(recs, "client_connects"),
                    "peak_rss_mb": _summarize(recs, "peak_rss_mb"),
                }
            )
    argv = " ".join(sys.argv[1:])
    metrics = {
        "generated_by": f"python scripts/gsv_throughput_bench.py {argv}",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "engine_head": head,
        "baseline_ref": args.baseline_ref,
        "machine": {"platform": sys.platform, "python": sys.version.split()[0]},
        "settings": {
            "requests_per_run": args.n,
            "batch_size": 100,
            "connection_limit": 50,
            "max_requests_per_minute": 48000,
            "rtt_ms": args.rtt_ms,
            "latency_distribution": "lognormal, sigma 0.5, mean per status",
            "warmup_excluded_s": args.warmup_s,
            "reps": args.reps,
        },
        "scenarios_ms": {k: {"zero_results": v[0], "ok": v[1]} for k, v in SCENARIOS.items()},
        "cells": cells_out,
    }
    out = Path(args.docs_dir) / "gsv-throughput_metrics.json"
    out.write_text(json.dumps(metrics, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {out}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("serve")
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--cov", type=float, required=True)
    p.add_argument("--ok-ms", type=float, default=0.0)
    p.add_argument("--zero-ms", type=float, default=0.0)
    p.add_argument("--sigma", type=float, default=0.5)
    p.add_argument("--cert", required=True)
    p.add_argument("--key", required=True)

    p = sub.add_parser("proxy")
    p.add_argument("--listen", type=int, required=True)
    p.add_argument("--target", type=int, required=True)
    p.add_argument("--delay-ms", type=float, required=True)

    p = sub.add_parser("client")
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--server-port", type=int, required=True)
    p.add_argument("--engine-root", required=True)
    p.add_argument("--n", type=int, required=True)
    p.add_argument("--rate", type=int, required=True)
    p.add_argument("--depth", type=int, default=0)
    p.add_argument("--old-pacer", action="store_true")
    p.add_argument("--batch-size", type=int, default=100)
    p.add_argument("--connection-limit", type=int, default=50)
    p.add_argument("--warmup-s", type=float, default=2.0)

    # The sweep is the default command; its flags live on the top-level parser.
    parser.add_argument("--baseline-ref", default="2a94a68")
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--n", type=int, default=12000)
    parser.add_argument("--rtt-ms", type=float, default=2.0)
    parser.add_argument("--warmup-s", type=float, default=2.0)
    parser.add_argument("--scenarios", nargs="+", default=list(SCENARIOS))
    parser.add_argument("--coverages", nargs="+", type=float, default=list(COVERAGES))
    parser.add_argument("--no-cpu-ceiling", dest="cpu_ceiling", action="store_false")
    parser.add_argument("--port0", type=int, default=8800)
    parser.add_argument("--docs-dir", default=str(REPO / "docs" / "experiments"))
    args = parser.parse_args()
    {"serve": _serve, "proxy": _proxy, "client": _client}.get(args.cmd, _sweep)(args)


if __name__ == "__main__":
    main()
