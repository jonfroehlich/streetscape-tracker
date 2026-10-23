# KartaView's nearby-photos endpoint moved, and the host that still answers no longer counts (2026-10-22)

**Decision: both KartaView channels are PAUSED on production from 2026-10-22** (`enabled = false` in `config/scheduler.makelab1.toml`, membership kept).
They come back with a collector fix, never with a one-line host change, because the host change alone would publish truncated censuses.

Numbers below trace to [`kartaview-endpoint-move_metrics.json`](kartaview-endpoint-move_metrics.json), produced by `scripts/kartaview_endpoint_probe.py --docs-dir docs/experiments` from the Mac on 2026-10-22 at 14:14 UTC.
The production facts are read from makelab2's catalog and `logs/` the same morning.

## What happened in production

- The last successful KartaView sweep was on the 2026-10-15 night: 32 collection attempts, none carrying a 404.
- No KartaView city was attempted on the nights of 10-16 to 10-21, so the change lies somewhere in that week and cannot be dated more closely.
- On the 2026-10-22 night all 124 KartaView attempts failed (62 cities × `kartaview` + `kartaview_streets`).
  Each spent 6 requests: the calibration ladder from r=1000 to r=100, every rung answered `404 page not found` (text/plain), and the collector raised "gave no usable answer at any radius or any calibration point… this is the endpoint, not the city's geometry".
  That is the intended path: a 404 is neither backpressure nor a host refusal, so it neither subdivided nor latched the night-level breaker.
- After that night 73 `kartaview` and 72 `kartaview_streets` pairs carried a failure, at most 1 each, against `max_consecutive_failures = 5`.
  Pausing stops that count rising toward quarantine.

No announcement exists anywhere we could find, which is what [`provider-access.md`](../provider-access.md) predicted for a provider with no staffed channel.
[facebook/Rapid#1139](https://github.com/facebook/Rapid/issues/1139) reported 404/301 from the same `1.0` path on `kartaview.org` about three years earlier, so routing on that host has broken before.

## What the probe measured

13 requests, at the anonymous 100/h pace (question 2 is whether the token buys the 1,000/h tier, so the probe does not spend on that assumption), all against the Seattle circle from the 2026-08-18 pagination measurement (47.6062, −122.3321).

**1. The path moved hosts; the API did not go away.**
`https://kartaview.org/1.0/list/nearby-photos/` answers 404.
`https://api.kartaview.org/…` and `https://api.openstreetcam.org/…` both answer 200, `apiCode` 600, and all 7 answering cases served the **same** 50 photos (one ids digest).
The JSON keys are unchanged.

**2. Neither `api.` host validates the token.**
A bogus token (zeros, the real one's length) and a garbage string got the same 200 and the same photos as the real token and as no token at all.
No `X-RateLimit-*` or `Retry-After` header came back, as before.
So nothing a client can see says whether a request is metered at 100/h or 1,000/h.
The collector paces at 16/min = 960/h on the assumption of the authenticated tier, and that assumption is now **unverifiable** rather than documented.

**3. `totalFilteredItems` no longer counts the circle — the blocker.**
On 2026-08-18 page 1 of this circle reported its exact total and pages 1–6 exhausted it.
Now every page reports `['0']`: page 1 served 200 photos, page 2 served 200 more with zero overlap, page 7 served none, and the total read 0 each time (both `api.` hosts).
Paging still works; the count does not.
The sweep sizes its paging from that count (`pages_for_total`, called in `download_kartaview.py`'s sweep loop): a total of 0 means one page.
Pointed at the new host as it stands, every circle holding more than one page (2,000 photos at production's `ipp`) would be read to its first page with **no error**, and the run would publish a lower coverage than KartaView holds.
That is worse than the honest 404s, and it is why the pause is the right state until the collector changes.

## What a fix needs

- `NEARBY_PHOTOS_URL` on `api.kartaview.org`.
- A total of 0 (or unparseable) beside a FULL page read as **unknown**, paging on until a short page, under the existing `MAX_PAGES_PER_CELL` cap — the cap's subdivide-or-record-unmeasured branches must still be reachable, or a dense circle pages without bound.
- Tests pinning the new response shape (`['0']` with a full page).
- The KartaView host lock, `provider-access.md` and the module docstring naming the new host.
- A decision on pacing given point 2; 960/h has nothing observable behind it now.

## Replicate

```bash
python scripts/kartaview_endpoint_probe.py --docs-dir docs/experiments   # ~8 min, needs KARTAVIEW_ACCESS_TOKEN
```

Refuses to run on a makelab host.
A partial run writes `kartaview-endpoint-move_metrics.partial.json` and never overwrites the committed record.

## Caveats

- One circle, one moment, one IP (the Mac). The total reading 0 on every page of a circle known to hold over 1,000 photos is not a sampling question, but whether some other request shape (a different `ipp`, the v2 photo API) still reports a total was not tested.
- Whether the old host's total had already gone to 0 before it started 404ing is unknowable now; the 10-15 sweeps succeeded, but nothing checked them for truncation.
