# Provider access: per-IP limits, budgets and blocks

The rate-limit and host-block corpus. **Read this before changing how, how often, how fast, or from
where we call any provider API** — that precondition is stated in `CLAUDE.md` and this file is what
it points at.

Split out of `CLAUDE.md` on 2026-08-22 so the always-loaded file stays under Claude Code's
size limit. The prose moved here is the original, with cross-reference pointers repaired where
they would otherwise dangle across the new file boundary; anything written since the split is
under its own heading and says so. `CLAUDE.md` keeps the short rule for each section and points
here for the evidence, the incident history and the details — keep the two in sync.

## The documented limit is not necessarily the binding one

**The documented limit is not necessarily the binding one, and the forum is where you learn that.** The 2026-08-12 incident is the case study: official docs say `tiles.mapillary.com` allows 50,000 requests/day **per app** and returns **4xx** when exceeded.
What actually happened was an **undocumented per-IP throttle** that redirected **302 → login** at 10,659 requests (~21% of the documented cap), blocking both of our Mapillary applications simultaneously from one host while the same token worked fine elsewhere.
A forum thread ([Inconsistent authentication issues](https://forum.mapillary.com/t/inconsistent-authentication-issues/5821)) already described this exact failure, already identified it as per-IP rather than per-token, and — critically
— already reported that **retrying during a block appears to extend it**.
Reading that thread first would have told us the ceiling was per-IP before we sustained 370 req/min into it, and would have flagged the retry hazard our nightly re-probing then ran straight into (whether that re-probing actually prolonged our block is a hypothesis, not a measured fact — see the recovery section below).
See also issue #205 and the Mapillary rate-limit sections below.

## Mapillary's binding limit is per-IP and undocumented (issues #198/#199/#241)

**Mapillary's binding limit is per-IP and undocumented — neither the documented daily cap nor, as #214 bet, a per-minute rate (issues #198/#199/#241).** Mapillary documents a 50k/day cap per *application*,
and that is not what actually stops us: on 2026-08-12 a bulk catch-up sustaining **~370 tile requests/min** got makelab2's **whole IP** redirected to a login page for every tile request, at a total spend of 10,659 — ~21% of that daily cap.
It is scoped to the **host**, not the credential: both Mapillary applications (`MAPILLARY_ACCESS_TOKEN` and `MAPILLARY_STREETS_ACCESS_TOKEN`) were refused simultaneously while the same token served tiles fine from another IP and `graph.mapillary.com` kept working
— so the `mapillary`/`mapillary_streets` channel split isolates *ledgers*, never this, and a second token or even a second Mapillary application would buy nothing.
`download_mapillary.py` therefore paces every tile request through `AsyncRateLimiter` at `DEFAULT_TILE_REQUESTS_PER_MINUTE` (60), overridable per channel via `[providers.*].max_requests_per_minute` and the `--mapillary-max-requests-per-minute` flag on both CLIs
— a **separate** flag from the GSV `--max-requests-per-minute`, whose value is a project-quota figure three orders of magnitude larger.
60 is a deliberately conservative guess (370 is confirmed too high) and slowness is cheap here
— but #241 proved 60/min alone is **not sufficient**: the 2026-08-20 block arrived while obeying it exactly, so the limiter is kept as a necessary bound (an unpaced burst is confirmed harmful, and a constant peak rate is what made the two incidents comparable) while the operative constraint is the multi-day accumulation window in the budget section below.
**The tile-count numbers, measured over all 1,214 enabled cities' frozen geometry on 2026-08-16** (`estimate_tile_count`; re-measure rather than trusting these, since grid re-registration moves them): median **12**, mean **57.8**, p90 **180**, p95 **306**, p99 **480**, max **870** (Moscow), and the **whole catalog is one 70,168-tile pass**.
So a 20-city night is ~1,160 tiles on the grid channel (~20 min) against the 10 h batch deadline, and a complete catch-up over every city is ~20 h of paced wall clock
— small as wall-clock, though the post-#241 budgets floor it at ~40 nights and catch-ups are paused besides (see the budget section below).
`scheduler.city_timeout_seconds` still **derives** a Mapillary timeout from `estimate_requests / rate` (`_TILE_ACHIEVED_RATE_FRACTION`, 0.8, since the limiter is a hard ceiling the fetch tracks closely, unlike gsv's project quota) instead of handing both Mapillary channels the flat `city_timeout_minutes` floor: pacing turned tile count into wall-clock, and a SIGKILL there costs the requests already spent *and* counts a failure.
Note that on today's geometry the derivation almost always resolves *to* that floor — 870 tiles is ~15 min against a 180-minute floor.
It did not always: pre-#166 Anchorage was ~6,480 tiles (~108 min) and is now 575, and an earlier version of this documentation quoted that as the live tail.
It is kept because a grid can be re-registered larger at any time, so the guard must not depend on today's caps holding.
Pacing and request counting live **inside** `_fetch_tile`'s retried body, so a tile that retries re-paces and is re-counted — one token, one ledger increment, one HTTP request;
taking the token in the caller let a retrying tile present up to `_TILE_MAX_TRIES`× the configured rate during exactly a 429/5xx storm, and under-report the same factor to `api_usage`.
**The limiter is per-process**, so N concurrent Mapillary collections would still present N× the rate
— which is why the host lock below, and not any ordering property of the scheduler, is what makes the configured figure the real one.
(That reasoning is what #240 relies on: a city's channels may now run concurrently, but the parent defers any channel whose per-IP host an in-flight sibling already holds, so the two Mapillary channels still never overlap and the tile CDN still sees one talker from this process — see the lanes section below.)
(Until #208 that guarantee rested on a rule for humans, and on 2026-08-14 a detached script that could not read it doubled the rate and got the host banned again.)
A block manifests as HTTP 302 → login whose *followed* page returns 200 `text/html`, so before #199 it reached the protobuf decoder and read as a corrupt tile; `_fetch_tile` now sets `allow_redirects=False` and names it.
**A blocked host also stops after the first refusal rather than paying for the whole city (issue #205).** `gather(return_exceptions=True)` (from #168, so one bad tile can't discard a city) must settle *every* tile before the settle loop can re-raise,
so a block used to spend the complete tile count at the paced rate to learn what response #1 already said — Fresno: 210 requests over 3.5 min, twice a night.
A `fatal` flag set in `fetch_one`'s `except DownloadError` now short-circuits the rest;
measured on a 361-tile city it cuts the spend to **1 request**, and the bound is `connection_limit` rather than a flat 1
— tasks already past the check are parked in the rate limiter and will issue theirs (which is why the test asserts `<= connection_limit`, read from the signature rather than copied).
Three details are load-bearing: the check sits **inside `async with semaphore`**, because `gather` starts all N tasks at once and each runs to its first suspension point
— above the semaphore every task would evaluate it before any response arrived;
only `DownloadError` trips it, so a per-tile 404/429/5xx still fans out to every tile and #168's guarantee is preserved by construction;
and `fatal` is **re-raised again after the gather**, which is unreachable today (the task that set it also re-raised, so its exception is in `settled`) but is what stops the abort from ever failing *open*: an aborted tile returns an **empty census**,
i.e. a clean success, so an edit that swallowed or wrapped that error would leave every tile empty, `failed_tiles` empty,
`detect_systemic_failure` unmoved (it only looks for REQUEST_DENIED/OVER_QUERY_LIMIT) and a 0-pano census registered, published, and diffed as "every pano in the city removed" — against an immutable dated snapshot.
A dedicated test simulates exactly that edit.
The two host-scoped classes (3xx redirect, HTML-on-200) raise `HostBlockedError`, which routes into #208's exit codes and night-level breaker;
**401/403 deliberately stays a plain `DownloadError`**, because a rejected token is scoped to the *credential* and the two Mapillary channels hold different ones, so typing it host-wide would let one channel's bad key stop the other.
Refused requests are still counted into `api_usage`: the request *was* issued, `count_request` firing before the status is known is #198/#203's "one token, one ledger increment, one HTTP request" invariant, and fail-fast drops the over-count from hundreds per city to at most `connection_limit`
— and, because #208's breaker skips that host's remaining channels once the first city reports a block, to at most `connection_limit` for the **whole night** rather than per city.
So #205's suggestion to stop counting them was declined rather than implemented; the drift it was worried about is now a rounding error against the daily budget.
Tests disable pacing suite-wide via an autouse `conftest.py` fixture, or a fixture city of a few hundred tiles would sleep for minutes.

## What Mapillary actually documents, and why none of it describes our block

**What Mapillary actually documents, and why none of it describes our block (read this before changing any pacing or volume knob).** The [API documentation](https://www.mapillary.com/developer/api-documentation) gives three limits: `graph.mapillary.com/:image_id` **60,000/min per app**,
`graph.mapillary.com/images?bbox=` **10,000/min per app**, and `tiles.mapillary.com` **50,000 per day — "(not per minute)" — per app, returning 4xx** when exceeded.
Read that parenthetical carefully: **the tile CDN has no documented throughput limit at all**; per-minute limits are a Graph API property.
Mapillary's own numbers are not even self-consistent — a staff reply on [Hitting request limit](https://forum.mapillary.com/t/hitting-request-limit/5820) gives graph as 50,000/min against the docs page's 60,000 + 10,000 (that thread is Graph API and a 403 "Application request limit reached", i.e. **not** our failure mode).
And our block matched the documented tile limit in **no** respect: wrong scope (per **IP**, both applications at once, where the docs say per app), wrong threshold (10,659 on 2026-08-12 and 5,013 on 2026-08-20
— 21% and 10% of the daily cap), wrong status (**302 → login**, not 4xx).
So the mechanism that hit us is undocumented in every attribute, and no budget or rate in this repo is derived from the docs — they are bets.
This is the corollary in `CLAUDE.md`'s READ THIS FIRST section in action: *treat any behavior you cannot find documented as unknown rather than unlimited.*

## A staff reply confirms the IP layer exists, and settles nothing else (2026-08-24)

**A Mapillary staff reply now says in as many words that a second blocking system runs at the IP level, underneath the documented per-app cap.**
Asked on [50,000 requests/day rate limit scope](https://forum.mapillary.com/t/50-000-requests-day-rate-limit-scope/10644) whether the tile limit is scoped per app, per account, per IP or globally, a staff member answered on **2026-08-24**:

> The mentioned 50k is by app ID.
> At the same time, if you use the same IP address and this is a sudden spike, the system might block you earlier.

Read against the section above, that changes exactly one thing.
The per-IP mechanism is no longer only a community report plus our own two incidents: a Mapillary employee describes it, and describes it as a distinct system that can refuse an address well below the documented per-app ceiling.
That is the first outside corroboration of what #198/#199/#241 worked out the expensive way, and it retires the "undocumented in every attribute" framing above for the *scope* attribute only — the threshold and the 302 status remain undescribed by anyone.

It does **not** settle which axis trips that layer.
"A sudden spike" is rate-flavoured language, and #241 falsified the pure-rate reading: the 2026-08-20 block arrived while the limiter was provably pinned at 60/min.
So take the phrase as confirmation that the layer exists and is IP-scoped, not as evidence about what provokes it; the rolling-window analysis in the next section still fits the ledger better than any rival tested against it.
**No pacing, budget or retry number in this repo moves on the strength of this reply**, and the corollary above is unchanged — a mechanism a vendor describes in one informal sentence is still one to treat as unknown rather than bounded.

The thread carries one lead this repo did not have: it directs commercial or production applications that need a higher quota to `support@mapillary.com`.
Nobody has written to them.
That is a decision rather than a task — it identifies this project to the vendor, and the quota it would ask about is not the one that has ever stopped us.

## The daily budgets encode a rolling 2–3 day per-IP window (issue #241, superseding #214's throughput bet)

**The daily budgets now encode the only constraint that fits the data: a rolling 2–3 day per-IP window (issue #241, superseding #214's throughput bet).** `[providers.mapillary].daily_request_budget` and `[providers.mapillary_streets]` are **1,750 each** (cut from 15,000 + 5,000 on 2026-08-22, and split evenly because both channels read the **identical z14 tile census** — a road walk re-reads the grid run's tiles
— so the two budgets deplete in lockstep and a heavy slate defers the same cities on both channels rather than un-pairing them).
The block is per **IP**, so the number that matters is the **sum** across the two channels
— different tokens, one address: **3,500/day**, chosen so any 2-day total stays ≤ 7,000, at or below the **highest value ever observed clean** (7,061).

How #214's bet resolved: it held that `max_requests_per_minute = 60` was the real protection and the daily budget a loose backstop, and it named its own falsifier
— a block on a night that never exceeded 60/min, with the day's `api_usage` and the elapsed hours captured.
**That is exactly what 2026-08-20 delivered** (the falsifier worked as designed): a second block with the limiter provably pinned at 60/min the whole way, at a per-IP day spend of **5,013**
— one day after **5,753 ran clean**, so a calendar-day cap is arithmetically impossible, and a pinned peak rate cannot be the discriminator because it is constant across blocked and clean days.
Testing every window against the ledger, only a **rolling 2–3 day accumulation** admits any threshold separating both blocks from every clean day: 2-day in (7,061, 10,766], 3-day in (10,284, 12,074]
— and the 2-day bound sits within 1% of the 10,659 single-day spend that caused the first block, consistent with an undocumented per-IP budget of roughly **10,000 per ~48 h**.
An equally good rival at n=2, and keep them distinct: a **repeat-offender penalty** (the IP carries a lowered threshold after a prior block), which predicts decay with clean time where a window does not.
Separating them costs a block if the experiment works, so it is deliberately not being run (Jon, 2026-08-22).
Full analysis, both incidents' numbers, and the staff statements: issue #241.

Consequences, in force until a rolling guard lands: **(1) no `--limit` catch-ups.** The daily budgets bound any single day, but three consecutive maxed days sum to 10,500 — inside the 3-day uncertainty band
— so a multi-night catch-up can reach where normal ~2–3k nights cannot.
The guard itself is a query change, not a schema change (`api_usage` is keyed (usage_date, provider) and already holds the history) — #241 item 3.
**(2) The budget, not `max_batch_hours`, now ends a maxed Mapillary night**: 3,500 is ~1 h of paced fetching against the ~17,000-tile deadline ceiling.
At the 57.8-tile mean each channel reaches ~30 cities/night and the 70,168-tile catalog is one full pass in **~40 nights**, not ~5; no city is ever skipped as over-budget (largest grid 870 < 1,750).
**(3) If a block ever arrives under this cap**, that is strong evidence for the repeat-offender reading over the fixed window
— capture the day's `api_usage` row, the elapsed hours from the `run-due` summary line (the `[alerts]` email carries it; nothing else records time-under-load), AND the trailing 3 days' ledger before changing anything.

## Interruption spend now survives on the Mapillary channels (issue #256)

**A Mapillary census that is interrupted no longer discards the tiles it already paid for.**
Before #256 a #205 fatal, a block, or a SIGTERM mid-census threw away every fetched tile and the next attempt bought them again
— against a channel budget of 1,750/day and a rolling window whose whole problem is accumulation, so the re-spend was not merely slow, it was charged twice into the constraint that produces blocks.
The census now resumes for its **missing tiles only**, through the same `checkpoints/` mechanism KartaView uses (#239) and the same caller-discards-after-the-row-lands lifecycle; the mechanics are in [`docs/census.md`](census.md).

Three things this deliberately does **not** change.
Resume is strictly **next-invocation**: no in-process retry is added anywhere, because the forum-reported hazard that retrying during a block extends it stands untested in either direction and is not worth testing with production credentials.
Pacing is untouched at 60/min, and so are both daily budgets — a resumed night is *cheaper*, never faster.
And the pre-flight estimate still prices the whole tile count even when a resume will fetch a fraction of it, which errs high; that is the safe direction for a budget gate and is left alone.

What it buys, concretely: tiles fetched before a block survive it, a crash between the CSV write and cataloging re-finalizes for ~0 requests, and a night the scheduler winds down mid-city resumes rather than restarting
— and it is what clears the resume gate on raising `max_concurrent_channels` above 1, since a deadline or SIGTERM under lanes kills up to N children at once instead of one.

## The supported way to run a bulk Mapillary catch-up

**The supported way to run a bulk Mapillary catch-up is `scheduler run-due --provider mapillary --limit N`, never a script.** Routing through the scheduler is the entire point: it inherits the daily budget ledger,
stalest-first ordering, per-channel `schedule_state` cadence and failure counting, the host lock, #205's fail-fast, #208's night-level breaker, alerting, orphan salvage and the publish tail.
The bespoke detached `setsid nohup` catch-up that had none of those is what got makelab2 banned by both Mapillary and Overpass in one night.
`--provider` takes enabled channel names (repeatable, or comma-separated) and **rejects an unknown, empty or `enabled = false` channel with exit `USAGE_EXIT_CODE` (64, sysexits.h's `EX_USAGE`) rather than running a zero-due night**
— on a host where Mapillary is switched off, silently accepting it would look like a successful collection while publishing nothing new.
That code is deliberately **not** 2: argparse already exits 2 on a parse error and `main()` ends with a catch-all `return 2`, so a wrapper could not tell a mistyped channel from an unknown subcommand.
`--limit` is validated the same way and for the same reason (`< 1` would collect nothing and exit 0).
An explicit `--limit N` **overrides `[schedule].max_cities_per_day`** for that invocation, because otherwise the config's 20 silently wins where the budget would allow ~30 cities;
the nightly systemd unit passes no `--limit` and is unaffected.
(Catch-ups are **paused** until #241's rolling guard lands — see the budget section above; the mechanism here is unchanged and stays the only supported path when they resume.)
`--limit N` does **not** truncate the candidate list to N — a candidate can be skipped without being processed (budget guard, host breaker, busy lock), so pre-slicing let the loop run out of list below N and report a clean night, which is this flag's own bug one layer down.

## A filtered run is not a narrower nightly run: it un-pairs the cities it touches

**A filtered run is not a narrower nightly run: it un-pairs the cities it touches.** `get_due_cities` derives dueness from `schedule_state.last_success_at` alone and never reads `day_of_cycle`, so a city's channels landing on one run date is a *consequence* of their clocks being in lockstep, not a constraint the scheduler maintains.
`--provider mapillary` advances only that channel's clock, so every city it collects stops sharing a run date with its other channels until their cadences happen to re-converge.
That is what catching a channel up *means* — but it is a real cost to the paired-snapshot property, so `cmd_run_due` logs a warning naming the channels left behind, and a test pins the behaviour.
Still ungoverned, deliberately: a manual `streetscape_tracker.py --provider mapillary` has no volume check at all (`cli.py` only ever *records* spend via `add_api_usage`), which is fine for the one-city case at a median of 12 tiles.

## Two `run-due` processes can overlap, and nothing serializes them

**Two `run-due` processes can overlap, and nothing serializes them.** Promoting the operator catch-up to a supported path makes "the nightly timer fires while a manual run is still going" an ordinary event rather than a mistake.
Most of the night tolerates it — the budget ledger is read live, `assign_schedule` is an idempotent upsert, and #208's host lock fails the *second* Mapillary child fast so the paced rate stays the configured one.
The exception was the catalog backup, whose staging file was named per *date*: two overlapping runs staged the same path, one unlinked the other's in-progress copy, and `verify → os.replace` then promoted a torn file as the day's verified backup with a clean "ok" in `backup_status.json` beside it.
`catalog_backup._staging_path` therefore names the **writing process** (host + pid), for both the copy and the status file, with abandoned staging files swept by *age* (`_STALE_TMP_AFTER_S`, above any live copy's `BACKUP_TIMEOUT_S`) rather than by name.

## Recovering from a block: measured twice — total silence, then ~a day

**Recovering from a block: measured twice — total silence, then ~a day.** What was an open problem after the first block now has the cleanest public measurement of this mechanism anywhere (nobody outside Mapillary had ever published one).
Both figures are upper bounds, and the measured/reported/guessed tiers below must stay distinct — do not let a later edit flatten them into more than they are.

- **Measured (block 1, cleared 2026-08-15).** Cleared after **~20.5 h of true silence**.
  The silence clock starts at the last *refused* request, not at the config edit: silence only began **~44 h after onset**, because a detached catch-up loop kept firing tens of thousands of refused requests after the channels were disabled;
  a probe at **~48 h was still refused**;
  and the clear came within the silence's first ≤20.5 h — so the block lasted at least ~48 h and at most ~65 h in total.
  Hence the standing rule: after disabling a channel, `ps`-sweep for detached loops before believing the host is quiet.
  The 2×2 that established per-IP scope (both applications refused from makelab2 while the same token worked from makelab1, same /24, and from a home IP; forcing IPv4 reaches the identical Meta edge and still 302s) was measured during this block.
- **Measured (block 2, cleared 2026-08-21).** With true silence from onset, cleared within **≤24 h 32 min**: blocked 15:13:38 on 08-20, first probe 15:45:27 on 08-21 returned 200/protobuf for **both** applications on the first attempt.
- **Staff, hearsay but the only inside figure ever given:** *"The block is 24 hours, as I remember"* (Jul 2026). #241's corroboration comment collects three staff acknowledgments from Dec 2025–Jul 2026: per-IP endpoint blocks exist, staff cannot easily check them, and the CDN limits are unpublished **by policy**
  — so no documentation will ever carry the number, and for bulk research use the staff-indicated channel is emailing Mapillary directly.
- **The rule both blocks fit: it lifts within ~a day of the last *refused* request.** A fixed 24 h from onset is refuted by block 1 (still refusing ~24 h in) unless refused requests reset the clock
  — which is operationally identical to the forum's "retrying seems to extend the block."
  That claim remains untested in both directions: both times we chose silence over the retry experiment, deliberately.
- **The protocol, proven twice:** disable both Mapillary channels **immediately**, sweep for detached processes, keep true silence, then a **single-request** probe per application daily starting ~24 h after the last refused request.
  An automated version (`probe_mapillary_block.sh` plus a self-disarming systemd user timer, untracked, emails the verdict either way) lives on makelab2 from the second incident.
  Re-enabling the channels stays a human decision — make it **promptly** on a clean probe, since both incidents' channels sat dark longer than their bans.
  And n=2 with upper bounds only: do not infer a shorter wait works, and do not probe more than once a day.

**Separately, a project decision rather than an empirical claim: makelab1 is NOT an escape hatch, even though it demonstrably still works** (verified 2026-08-13: same token, same /24, 200 + 12.4 MB while makelab2 got 302).
**Project Sidewalk serves Mapillary data off the makelab servers**, so pointing this workload at makelab1 risks earning the same per-IP block on a host that a *production research deployment* depends on.
Jon has ruled this out; trading Project Sidewalk's imagery for our nightly batch is never the right trade.
If collection must move, it moves to a host with nothing else riding on it — and only after reading the forum first (see the top-of-file rule).

## Per-IP hosts and the cross-process host lock (issue #208)

**Per-IP hosts and the cross-process host lock (issue #208).** Three third parties are **locked** because they meter us by **IP rather than by credential**
— Mapillary's tile CDN, `overpass-api.de` and `kartaview.org`
— and a per-IP limit is a property of the whole machine, so no per-process limiter can honour it alone.
(Two more are per-IP and knowingly *unlocked*, named in `download_common.py` so the list doesn't read as exhaustive: Nominatim in `geoutils.py`, which runs only when an unknown city is registered and once per `assess-city` invocation (its boundary pre-flight, #215
— even for an already-registered city), and `download_gsv_history.py`'s undocumented `SingleImageSearch`, which is IP-identified rather than key-metered and already carries its own circuit breaker.
Both are out-of-band and low-volume; lock them if either ever runs on a schedule.)
`streetscape_metadata_tracker/host_lock.py` supplies the missing half: `host_lock(host)` takes a `filelock` on `locks/{host}.lock` for the duration of one fetch, `timeout=0` so a second process **fails fast** (following `download_gsv.py`'s run-level lock) rather than queueing behind a multi-hour run.
Chokepoints are the two places every such request in the repo passes through: `download_mapillary.fetch_city_images_async` (grid run *and* road walk) and the download branch of `download_street_network.fetch_graph`
— placed **after** its cache-hit return, so a warm city never contends, and **outside** `_download_graph` so one hold covers the whole retry stack.
GSV metadata is deliberately **not** locked: Google meters the Street View Static API per *project*, so two processes share a quota the daily ledger already tracks and serializing them would cost throughput for nothing.
**Only the child ever holds the lock** — `flock` is scoped to an open file description and is not inherited across `subprocess.run`, so a scheduler parent holding it would make every child's `timeout=0` acquire fail;
a source-inspection test asserts `scheduler.py` never imports the module.
A stale lock file cannot wedge a night, because the kernel releases `flock` when the fd closes (SIGKILL and OOM included)
— the `.lock` file on disk is not a held lock, only the `.owner` sidecar naming the holding pid can go stale, and the busy message says so.
Where the file lives is the subtle part, and all three constraints bite at once on makelab2: **not `/tmp`** (the unit sets `PrivateTmp=true`, so the scheduler's children and a detached operator shell would not see each other's locks
— precisely the pair this exists to serialize), **not the unresolved checkout path** (the unit's `WorkingDirectory=%h/streetscape-tracker` is a symlink and `paths.get_project_root()` uses `abspath`, which does not resolve it, so the realpath is taken
— of the `STREETSCAPE_LOCK_DIR` override too, not just the default, or an operator exporting the `~` spelling of the same directory silently takes a *different* lock while believing they hold the same one), and **not under `data/`**, which the publisher rsyncs to a public web server.
**The exit status carries two facts, not one**, because the child's message never crosses the process boundary — the scheduler sees only `returncode`
— and the two host conditions have opposite lifetimes: `HOST_EXIT_CODES` (75 Mapillary tiles, 76 Overpass, 81 KartaView) means the third party **refused this IP**, while `HOST_BUSY_EXIT_CODES` (79/80/82, deliberately past the end of `sysexits.h` so they carry no false analogy) means **another local process holds the lock**.
`host_exit_code()` is the one place that distinction becomes a number.
`_run_city_loop` turns the first *refusal* into a **night-level breaker**: that host's channels (`CHANNEL_HOSTS`) are skipped for the rest of the run, since the condition is a property of this machine and asking again with the next city cannot answer differently.
A *busy* exit deliberately does **not** trip it — that condition ends when the other process does, so escalating it would let a two-minute manual Mapillary run cost the batch every Mapillary city of the night;
it skips one channel of one city and the next city asks again.
Critically, **neither** kind of skip records `record_attempt(success=False)`
— `get_due_cities` filters on `consecutive_failures < max_consecutive_failures` (5) and *nothing in the codebase resets that counter except a success*, so a handful of such nights would quietly quarantine cities for a whole 90-day cycle, recoverable only by hand-written SQL.
The cities stay due and lead the next night's stalest-first queue instead.
Because nothing is counted as a failure, `_finish_batch` is what makes such a night visible: it alerts **unconditionally, ignoring `[alerts].failure_threshold`** (the failed-backup posture), names the host and which of the two conditions it was in the subject (`host(s) UNAVAILABLE` vs `channel(s) SKIPPED (host busy)`
— the operator's next move differs: wait out a ban, or go find the stray process), exits nonzero — and still publishes (#167).
Tests neutralize the lock suite-wide via an autouse `conftest.py` fixture pointing `STREETSCAPE_LOCK_DIR` at a per-test `tmp_path`, so pytest can run during a real nightly batch;
`tests/test_host_lock.py` then opts back in by taking a second `FileLock` on the same path, which behaves exactly like a competing process inside one pytest process.

## Overpass is a per-IP volunteer service, and the fetch is hardened accordingly (issue #209)

**Overpass is a per-IP volunteer service, and the fetch is hardened accordingly (issue #209).** It is on the critical path for essentially every road walk
— **1134 of 1144 enabled cities have no cached GraphML**, so a first walk always goes to the network
— and on 2026-08-14 `overpass-api.de` firewall-banned makelab2, turning every walk into a bare `tenacity.RetryError` after minutes of retries with nothing naming Overpass.
Five things follow from that.
**(1) `ox.settings.timeout = 60` had never done anything**: osmnx 2.x renamed it to `requests_timeout` and `ox.settings` is a plain module, so the assignment silently created an unread attribute and every call ran at the 180 s default.
The fix sets `OVERPASS_TIMEOUT_S = 180` — *not* the intended 60
— because the same value is interpolated into the **server-side** `[timeout:{}]` clause of the Overpass QL header (`_make_overpass_settings`), so lowering it would start server-aborting the large-bbox fetches that succeed today;
a test pins that coupling.
**(2) The retry policy retried everything.** With no `retry=` predicate, tenacity's default re-asked settled answers — a ban page, a bbox with genuinely no drivable ways
— three times, and with no `reraise=True` the caller saw `RetryError` instead of the cause.
Now only `ConnectionError`/`Timeout` retry, and the real exception survives.
**(3) Failures are typed, and the host/city split is load-bearing**: `_download_graph_named` raises `HostBlockedError(host=overpass)` for a refusal (tripping #208's breaker) but a **plain `DownloadError`** for `InsufficientResponseError`, because one roadless village must not cancel the night's other nineteen cities.
(Known gap, documented at `_download_graph_named` rather than fixed: osmnx raises `InsufficientResponseError` **both** for "no data elements" and for "HTTP 200 whose body isn't JSON" (`_http._parse_response` picks that type precisely *because* the status was ok), so a captive portal or error page served with a 200 reads as a city failure and all 20 cities re-ask
— structurally the same bug #199 fixed for Mapillary tiles. osmnx doesn't hand the caller the response, so any fix is a message sniff; the pre-flight is the mitigation that catches the realistic version.)
**(4) A `/status` pre-flight** names a refusal in ~1 s instead of after three timing-out attempts.
It is **advisory only**, and the bar for saying "refusing" is deliberately high because the error is asymmetric: a false negative costs one wasted fetch that produces the real error anyway, a false positive skips **every street channel of every city** for the night.
So refusal is an **allow-list** (`403/429/509`) rather than "anything that isn't 200"
— a 502/503 from a front end while `/interpreter` is healthy is ordinary here
— a queued slot is **not** a refusal at all (osmnx reads the same endpoint in `_get_overpass_pause` and simply sleeps the wait off, so cancelling would cancel a fetch that was going to succeed),
and the **whole body** is wrapped in `except Exception`, since `ox._http` is private under an unpinned `osmnx>=2.0` and an `AttributeError` from an advisory probe would fail every street collection on the machine, inside the host lock, before any real request.
It must send our headers, because `overpass-api.de` answers **HTTP 406 to the stock `python-requests` User-Agent** (measured 2026-08-15; every other UA returned 200), so a probe using the default would have read that 406 as a block and skipped every city of every night.
It reuses `ox._http._get_http_headers()` so the probe is indistinguishable from the query it speaks for.
It is a **second** `/status` GET on top of osmnx's own, deliberately: osmnx's answers "how long until a slot?" and never short-circuits, `/status` is unmetered, and the duplication is the price of the fast refusal.
**(5) osmnx's 429/504 handler recurses without a depth limit** (`_overpass.py:477-486`: `time.sleep(55)` then re-call itself), so a rate-limit-flavoured refusal never fails
— it hangs until the scheduler SIGKILLs the child, and a SIGKILL carries **no exit code**, so the breaker never learns.
`_deadline(OVERPASS_DEADLINE_S)` (SIGALRM, no-op off the main thread or without SIGALRM) bounds it; a real 504 was observed on 2026-08-15, so this is live.
The alarm raises a private `_DeadlineExceeded`, not a bare `TimeoutError`
— the builtin **is** `socket.timeout`, so catching it would report a stray socket timeout as "repeated 429/504" and send an operator after the wrong thing.
`OVERPASS_DEADLINE_S` is **derived** (`3 * (OVERPASS_TIMEOUT_S + 120)` = the same 900) so it can't silently fall below the worst legitimate fetch: three attempts, each a full request timeout plus osmnx's pre-request slot pause, which with `overpass_rate_limit = True` is the entire wait the server advertises.
If it ever fires on healthy-but-busy nights, raise the per-attempt slack — do not remove the bound, since unbounded is how the SIGKILL happens.
Also: we now identify ourselves per the usage policy (`http_user_agent`/`http_referer`, matching what `geoutils.py` already does for Nominatim);
`OVERPASS_URL` overrides the endpoint for an incident-time mirror and is read at **call** time (`_apply_overpass_url`, re-run per `fetch_graph`) so the 03:00 escape hatch is settable without a restart and testable at all;
and the legacy `gsv_street_analyzer/` copy is **deleted** rather than patched
— it was referenced nowhere but had an argparse CLI and a `__main__` guard, i.e. a runnable Overpass client with no host lock, no UA and no deadline, which is exactly the "process that cannot read the rule" this stack exists for.
**Deliberately no new client-side pacer** — Overpass's own guideline is "fewer than 10,000 queries/day", we issue roughly 20 a night with `sleep_between_cities_s` already between cities, and the ban came from concurrency (#208) and plausibly from (5), not from our steady-state rate.
Tests stub the probe suite-wide via an autouse `conftest.py` fixture, or every street test would hit a volunteer-run service from every dev machine and CI job.

## Pointers added after the split

The backup-side rule from "Two `run-due` processes can overlap" — that
`catalog_backup._staging_path` names host + pid and sweeps by age — is restated where someone
changing `catalog_backup.py` will actually be reading, in
[`catalog-backups.md`](catalog-backups.md). Keep the two in sync.

## Concurrent channel lanes leave per-host presentation invariant (issue #240)

**Added after the split.**
`[schedule].max_concurrent_channels` lets one city's channels run at once (default 1 = the historical back-to-back behaviour).
**Nothing about what a provider sees changes**, and that claim rests on three things rather than on good intentions.
**(1) Host affinity is enforced in the parent.**
The launch pass derives the per-IP hosts its in-flight children hold from `CHANNEL_HOSTS` and defers any channel that intersects them, so Overpass, the Mapillary tile CDN and KartaView each see **at most one talker from this process at a time** — the same as before.
The child-side cross-process lock (#208) is untouched and still covers the manual runs the parent cannot see.
**(2) Pacing and budgets are per channel and unchanged**: each child keeps its own limiter, and the combined Mapillary per-IP ceiling stays 3,500/day.
The binding Mapillary constraint is multi-day *volume* (#241), which intra-night packing does not move — a night collects the same cities and spends the same tiles, sooner.
**(3) The only observable change is wall clock.**
The one place this is not free is the pair Google meters per Cloud **project** rather than per IP: `gsv` and `gsv_streets` declare no host, so nothing serializes them, and running them concurrently presents 48k + 24k req/min.
That is safe **only if the two keys live in separate projects**, which this repo does not record anywhere — it is a Google Cloud Console check before the knob is raised, and a shared project must be fixed by splitting the keys rather than by declaring a fake host (a fake `CHANNEL_HOSTS` entry would couple the night-level breaker to something that is not a host refusal).
Raising the knob was also gated on **resume for every provider**, because a deadline or a `systemctl stop` kills N children at once instead of 1 and a killed Mapillary child re-spent its tiles into the ceiling this file exists to defend.
**That gate is met as of #256** (see the checkpoint section above): every channel now resumes, so the Cloud-project check is the one that remains.
Mechanism, rollout order and the watch list: [`scheduler.md`](scheduler.md).
