# KartaView's nearby-photos endpoint moved, and the host that still answers no longer counts (2026-10-22)

**Decision: both KartaView channels are PAUSED on production** (`enabled = false` in `config/scheduler.makelab1.toml`, membership kept).
The endpoint had broken by the 2026-10-22 night; the pause was applied by hand on 2026-10-23 at 06:30 PDT, after a second night of failures, and committed here.
They come back with a collector fix, never with a one-line host change, because the host change alone would publish truncated censuses.

Numbers below trace to [`kartaview-endpoint-move_metrics.json`](kartaview-endpoint-move_metrics.json), produced by `scripts/kartaview_endpoint_probe.py --docs-dir docs/experiments` from the Mac on 2026-10-22 at 14:14 UTC.
The production facts are operator readings from makelab2's catalog and `logs/`, recorded by hand in [`kartaview-endpoint-move_production.json`](kartaview-endpoint-move_production.json) with the read-only command behind each figure, since no committed script produces them; the probe never touches that file.

## What happened in production

- The last successful KartaView sweep was on the 2026-10-15 night: 32 collection attempts, none carrying a 404.
- No KartaView city was attempted on the nights of 10-16 to 10-21 (recorded as six zero nights in the production file), so the change lies somewhere in that week and cannot be dated more closely.
- On the 2026-10-22 night all 124 KartaView attempts failed (62 cities × `kartaview` + `kartaview_streets`), and on the 2026-10-23 night all 136 (68 cities × 2).
  Each spent 6 requests: the calibration ladder from r=1000 to r=100, every rung answered `404 page not found` (text/plain), and the collector raised "gave no usable answer at any radius or any calibration point… this is the endpoint, not the city's geometry".
  That is the designed path for a 404: it is neither backpressure nor a host refusal, so it neither subdivided nor latched the night-level breaker.
  But it is not amnestied either: each failed attempt counts a per-city `consecutive_failures`, and fast failures fill the city cap.
- That is what the second night cost: 21 of 157 runs succeeded, and the 80-city cap was spent in 1.60 h on 6-request failures, leaving about ten hours of the night unused.
- After the two nights 62 pairs on each channel stand at 2 consecutive failures and 17/16 at 1, against `max_consecutive_failures = 5`.
  Pausing stops that count rising toward quarantine.

No announcement exists anywhere we could find, which is what [`provider-access.md`](../provider-access.md) predicted for a provider with no staffed channel.
[facebook/Rapid#1139](https://github.com/facebook/Rapid/issues/1139) reported 404/301 from the same `1.0` path on `kartaview.org` about three years earlier, so routing on that host has broken before.

## What the probe measured

12 requests, at the anonymous 100/h pace (question 2 is whether the token buys the 1,000/h tier, so the probe does not spend on that assumption), all against the Seattle circle from the 2026-08-18 pagination measurement (47.6062, −122.3321).

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
Now every page probed reports `['0']`: on `api.kartaview.org` page 1 served 200 photos, page 2 served 200 more with zero overlap, page 7 served none, and the total read 0 each time; `api.openstreetcam.org` was asked only page 1, which read 0 as well.
Paging still works; the count does not.
The sweep sizes its paging from that count (`pages_for_total`, called in `download_kartaview.py`'s sweep loop): a total of 0 means one page.
Pointed at the new host as it stands, every circle holding more than one page (2,000 photos at production's `ipp`) would be read to its first page with **no error**, and the run would publish a lower coverage than KartaView holds.
That is worse than the honest 404s, and it is why the pause is the right state until the collector changes.

## What a fix needs

- `NEARBY_PHOTOS_URL` on `api.kartaview.org`.
- A total of 0 (or unparseable) beside a FULL page read as **unknown**, paging on until a short page, under the existing `MAX_PAGES_PER_CELL` cap — the cap's subdivide-or-record-unmeasured branches must still be reachable, or a dense circle pages without bound.
- Tests pinning the new response shape (`['0']` with a full page).
- The KartaView host lock's comments, `provider-access.md` and the module docstring naming the new host (the lock key itself is `HOST_KARTAVIEW = "kartaview"`, a lock name rather than a host name).
- A night-level stop for a moved endpoint.
  The collector already refuses a whole ladder of 404s by name ("this is the endpoint, not the city's geometry"), but that exit is 1, a counted failure: it stamps `consecutive_failures`, and because the city was attempted it takes a city-cap slot, which is how 10-23 spent the cap in 1.6 h and walked every pair toward quarantine.
  The Panoramax lattice refusal (#407) is the same shape and counts the same way.
  What stops both is an amnestied exit family that latches the channel for the night, so the remaining KartaView launches are skipped and a KartaView-only city takes no slot.
  The #444 throttle (87) is the one-night shape; a blocked host (75/81) also writes a `host_refusals` row, which arms a refusal cooldown only where `[hosts.<host>].refusal_cooldown_h` is set (today only the Mapillary tile CDN, 36 h), and the fix should pick between the two on purpose.
- A decision on pacing given point 2; 960/h has nothing observable behind it now.

## Replicate

```bash
python scripts/kartaview_endpoint_probe.py --docs-dir docs/experiments   # ~7 min, needs KARTAVIEW_ACCESS_TOKEN
```

Refuses to run on a makelab host.
A partial run writes `kartaview-endpoint-move_metrics.partial.json` and never overwrites the committed record.

## Caveats

- One circle, one moment, one IP (the Mac).
  The total reading 0 on every page of a circle known to hold over 1,000 photos is not a sampling question, but whether some other request shape (a different `ipp`, the v2 photo API) still reports a total was not tested.
- Whether the old host's total had already gone to 0 before it started 404ing is unknowable now; the 10-15 sweeps succeeded, but nothing checked them for truncation.

## Resolution (issue #452, 2026-10-27)

Nothing new was measured for this section; it records what the code does now and what is still open.

**What changed.**

- `NEARBY_PHOTOS_URL` is `https://api.kartaview.org/1.0/list/nearby-photos/` (`api.openstreetcam.org` is the noted fallback).
- A total smaller than the page it arrived with is UNKNOWN (`total_is_usable`), and the circle is paged blind until a short page (`page_is_full`), under `MAX_PAGES_PER_CELL`; past the cap it subdivides, or at the radius floor records the cell unmeasured.
  A blind page is FULL at `min(ipp, IPP_MAX, MEASURED_BLIND_PAGE_SIZE)`, the last being **200, the only page size measured on this host** (the 2026-10-22 probe asked at `ipp=200`); `IPP_MAX` (2,000) is Grab's figure for the old host and was never measured here (#452 review).
  So the stop condition holds for any server page cap of 200 or more, at the cost of one extra request (an empty page 2) for a blind circle of 200–1,999 photos if the host does honour 2,000.
  Each sweep logs `N cell(s) paged blind`, of which `M` paid a page 2, and returns `cells_paged_blind` and `cells_paged_deep`.
- An HTTP 404 is `RouteNotFoundError`, checked before the HTML test.
  404s at every calibration probe, or at the first `ENDPOINT_NOT_FOUND_STRIKES` (6) requests of a sweep that skipped the ladder with nothing answered, raise `HostEndpointMovedError`: exit **89** for KartaView.
  Panoramax's whole-lattice 404 (#407) raises it too, as exit **90**, and so does an all-404 resume; both only at `ENDPOINT_MOVED_MIN_TILES` (6) tiles or more, so a one- or two-tile city stays its own refusal (#452 review).
- The scheduler treats 89/90 like the #444 throttle: the host is latched for the night, the child is amnestied (no `consecutive_failures`), the branch skips `attempted += 1` so a city due only on that host takes no city-cap slot, and no `host_refusals` row or cooldown is written.
  The alert names the moved endpoint, the probe script and the pause.
- The default pace is the anonymous tier, **1.5/min = 90/h**, a float through both CLIs and `ProviderConfig`; both config files pin it.
  16/min is kept as `AUTH_SWEEP_REQUESTS_PER_MINUTE`.
- Each KartaView launch is held to a per-city wall-clock ceiling, `[providers.kartaview*].max_city_minutes`, default **60** (#452 review), because the city loop is sequential and a launch sized to the night's remainder held every later city's Mapillary and GSV lanes for ~6 h at this pace.
  The cap that follows is ~37 requests a launch (priced at the 0.5 achieved fraction), which a child at the full 40 s interval spends in ~25 min before pausing with exit 83.

**Still open.**

- The pace is not decided: 1/min keeps every type an integer at 40% less throughput, and 100/60 hits the documented figure exactly.
- At 90/h a Singapore-class sweep (~9,974 requests, ~111 h) cannot finish inside the 7-day `CHECKPOINT_MAX_AGE_S`, so the scheduler refuses its resume and records a failure by design; the wall is not raised here.
- Whether the authenticated tier returns: only a probe showing a bogus token refused would say so.
- How often to re-probe `kartaview.org` (the probe script, from a laptop); `probe-host` has no KartaView entry.
- Whether the 10-15 sweeps were already truncated.
- **Whether `api.kartaview.org` honours `ipp=2000`, and pages by its own page size.** The blind stop no longer depends on the first (see above), but if the host capped a page below 2,000 while offsetting page 2 by the 2,000 asked for, rows between the two would be skipped. A two-request laptop probe at `ipp=2000` against the Seattle circle (page 1 carries 2,000 items, page 2 is non-empty and disjoint) closes both; it was not run here, since no live request was allowed.
- `_SWEEP_OVERHEAD_MULTIPLIER` (1.80×) is not re-measured for blind paging, which costs one extra page for an exactly-full circle and up to `MAX_PAGES_PER_CELL − 1` more per subdivided root.

**The resume plan (prod steps, in order).**

1. Deploy main to makelab2.
2. A config PR flipping `enabled = true` on both KartaView blocks of `config/scheduler.makelab1.toml` (`test_makelab1_production_config_is_wired` flips with it).
3. Before the flip, read `scheduler status` for the enrolled KartaView cities and price the first night's shape: at most `opt_in_cities_per_day` hoisted KartaView cities, each holding the city loop for at most one `max_city_minutes` per channel (two for a city due on both), so 10 cities at the default bound KartaView to ~10–20 h of loop time in the worst case and ~4–8 h if each child spends its ~37-request cap at the full pace. That worst case is a whole night, which is why the ceiling's value is an open decision rather than settled here.
4. After night 1: `grep -h "paged blind" logs/collect_*_kartaview_*` and read the `Done:` line.
5. Compare every re-collected 10-15 city's pano count against its previous run (`runs` rows per city and provider). On this host every visited cell is paged blind, so `cells_paged_blind` > 0 says nothing (#452 review); the signal is `cells_paged_deep`. A collapse over 30% in a city whose `cells_paged_deep` is 0 means no circle ever filled a page, which is what a page cap below 200 would look like.
6. An `ENDPOINT MOVED` alert means `api.kartaview.org` moved too: pause again and re-probe from a laptop.

The 62 pairs at 2 of 5 `consecutive_failures` reset on their first success; no `reset-failures` is needed.
