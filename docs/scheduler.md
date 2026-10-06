# Scheduler

The nightly batch: dueness, the tail, wind-down, deadlines, timeouts and the dead-pipe rules.
Read before touching `scheduler.py`, the systemd units, or anything about how a night ends.

Split out of `CLAUDE.md` (2026-08-22); the router keeps this topic's short rules and points here for the evidence and detail.
An edit that changes a rule belongs in both files; anything written since the split is under its own heading and says so.

## `scheduler.py`, the nightly batch and its tail

**Scheduler** (`streetscape_metadata_tracker/scheduler.py`): designed as a systemd user timer on makelab1 (units + install docs in `deploy/`).
`run-due` collects cities whose last success is ≥ cycle_days − grace_days old (stalest first);
a due city runs all enabled providers on the same run date (paired snapshots)
— back-to-back by default, or concurrently in host-disjoint lanes when `[schedule].max_concurrent_channels` > 1 (issue #240; see the lanes section below) —
each as a `streetscape_tracker.py --provider X` subprocess within its own daily budget (`[providers.gsv]`/`[providers.mapillary]` in scheduler.toml; a legacy toml without `[providers]` runs gsv-only).
Then regenerates the aggregate once and publishes.
Stagger = `sha256(city_id) % cycle_days`, identical for all providers of a city.
`run-due --provider CHANNEL [--limit N]` narrows one invocation to a subset of the enabled channels (issue #214)
— the filter is applied in `_collect_due`, whose `providers` argument is **required** (a None-means-everything default put a fail-open path one refactor away from `_select_providers`'s error return, which is now a raised `_UsageError`),
so a channel absent from `providers_for_city` is never priced, budgeted or launched, and everything else about the night (backup, driving-plan hook, breaker, tail) is unchanged.
It is not free of consequences, though — see the paired-snapshot note in the Mapillary budget section of `docs/provider-access.md`.
`run-due --city CITY` (repeatable, never comma-split — queries carry commas) narrows it further to named cities, for a targeted retry that keeps every guard above rather than being run as a bare collector command.
Without `--limit`, the number of named cities replaces `max_cities_per_day` as the cap, so a 50-city list is never silently cut to 40; with an explicit `--limit` below the count, the named cities past it are logged by name.
It is applied in `_collect_due` to each channel's due list BEFORE the union, so both reservations and the logged `hoisted`/`promoted` counts describe the slate that actually runs.
It **narrows and never forces**: a named city that is not due (fresh clock, failure cap, excluded, disabled) is warned about by name and skipped, and an unresolvable name exits 64 before any schedule write.
The motivating case was 2026-09-24: Detroit's and Fresno's Mapillary walks had each failed once during the August block and sat at queue positions 66 and 76 of 271, deferred behind the alphabetical never-collected block, with no supported way to reach them.
The STRANDED alert (#341) prints this command with the ids filled in, one per exact set of lost walk channels: a city stranded on one walk because its OTHER grid run failed is still due on that other walk, and a single combined command would pay its census for an un-paired walk (#362).
It used to print `run-due --provider <walk> --limit N`, which walks the stalest-due queue — on 2026-09-22 none of 8 stranded cities was in the first 10 of any walk channel, and Austin's ~640k-request walk led `gsv_streets`.

**A hand `run-due` that would collect a GSV key another `run-due` on this host is collecting exits 64; the nightly unit is never refused, and alerts instead** (issue #412).
Since #304 each GSV process reaches its configured 48,000/min, so a hand catch-up on the same key as the nightly presents ~96,000/min against a 60,000/min project quota, and nothing serializes GSV across processes (Google meters per project, so `CHANNEL_HOSTS` gives it no host lock).
`run-due` reads its rate from config and has no per-run override, so waiting is a hand run's only safe action; the direct CLIs can be slowed instead (the pre-run checklist in `docs/operations.md`).
`assess-city` spends the `gsv_streets` key the same way and is refused the same way (not under `--estimate`).

- **What counts as an overlap** (`_gsv_key_overlaps`): `_scan_run_due_processes` reads `ps -ww -e -o pid=,ppid=,args=` and keeps a line only when its argv is a python executable, then `-m streetscape_metadata_tracker.scheduler`, then a `run-due` token — so a `pgrep -f` loop, a `tail -f logs/run-due.log`, `--config run-due.toml status`, a shell or `timeout` wrapper and a pytest `-k run-due` never match.
  The args column is split on whitespace, never `shlex`: `ps` quotes nothing, and `shlex` read two apostrophes (a `--city Coeur d'Alene` beside an apostrophe in `--config`) as one quoted span that swallowed the `run-due` token.
  `ps` output is decoded with `errors="replace"`, so a non-UTF-8 argv anywhere in the table cannot raise out of the pre-flight.
  This process and its whole ancestor chain are excluded.
  The other process's channels are read from its argv (`--provider X`, `--provider=X`, repeated or comma-separated; none means this config's enabled set; `--dry-run` means none), and only a shared GSV key refuses — `gsv` and `gsv_streets` are separate keys, so a `gsv_streets` walk beside a `gsv`-only catch-up is no overlap.
  Exact tokens suffice because the `run-due` subparser sets `allow_abbrev=False`: `--prov gsv` or `--dry` exits 2 at parse time, so no live `run-due` can hide its channels behind an abbreviation.
- **The nightly is identified by either of two independent signals** (`_is_nightly_unit`): `STREETSCAPE_NIGHTLY=1`, which the unit file sets, or `/proc/self/cgroup` holding a path that ends in `/streetscape-tracker.service`.
  `INVOCATION_ID` would not do, since every unit and any `systemd-run --user` sets it.
  Two because a misidentified nightly fails in the dangerous direction: it is refused as a hand run (exit 64) and the night is lost.
  Neither had been read on prod when this shipped; the installed unit is a copy, so the variable is live only after the copy is refreshed and `daemon-reload`ed (`deploy/README.md`), and until then the cgroup check carries it alone.
  A refused `run-due` says it was NOT identified as the nightly and prints the variable's value and the cgroup lines it read (or the `OSError`), so a misidentified night explains itself in the scheduler log.
  On an overlap the nightly logs a warning, sends one `[alerts]` email naming the other pid and command line, and proceeds on every channel — refusing it would lose all eight channels' night to protect one key.
  No `/proc` (macOS) and no variable reads as "not nightly".
- **A refused hand run reports only to its terminal**, which is enough because its operator is the one watching it: `deploy/systemd/streetscape-tracker.service` ships with `OnFailure=` commented out, and the notify unit is not installed on makelab2, so no email follows an exit 64.
  The same holds for a misidentified nightly: its refusal reaches the scheduler log and the unit's console log, and no email.
- **It refuses for the other process's whole life, not just its GSV lanes.** The check sees that another `run-due` is alive, not which channel it is on, so a hand run is refused until the nightly exits (~12-14 h), even after the nightly can no longer reach that key.
  The refusal says so: the other process may already be past its GSV channels, so check the scheduler log for whether it is still on `gsv`/`gsv_streets` (its latest `Collecting … [gsv]` / `[gsv_streets]` launches, or a tail already under way), and if it is not, re-run with `--force`.
  Lane-state tracking that would answer this in code was deliberately not built; the operator's read of the log is the mechanism.
- **Exempt:** `--dry-run` (a preview spends nothing) and any run holding no GSV key (`--provider mapillary --limit 5` never reads `ps`, since `host_lock` already serializes every per-IP host).
  `--force` overrides a match known not to be collecting that key, with a warning naming it.
- **Blind spots.** It is per-HOST: a same-key run on another machine (a laptop with the prod key) is invisible.
  It sees `run-due` only: a direct `streetscape_tracker.py` or `collect --provider gsv` run, or an `assess-city`, is invisible to a later `run-due`, and is not itself guarded — the checklist covers those.
  It assumes the other `run-due` reads the same config when it names no `--provider`.
  It fails OPEN when `ps` is unavailable, and it is a check at start only: a hand run started before the 02:00 timer is not stopped, the nightly just alerts.


**Every channel is paced, so every channel's per-city timeout is DERIVED rather than flat, and `city_timeout_minutes` (180) is only the floor.**
The shape is the same for all of them — `estimated_requests / (rate × achieved_rate_fraction) × _TIMEOUT_HEADROOM + _TIMEOUT_FIXED_SLACK_S`, never below the floor — and what differs is where the request count and the rate come from.
`gsv` prices grid points against `[download].max_requests_per_minute`.
`gsv_streets` prices on-street samples against **its own** `[providers.gsv_streets].max_requests_per_minute`, falling back to `[download]`'s only when that key is unset — the two have been numerically equal at 48,000 since 2026-09-21, so the distinction is no longer visible in behaviour, but it is still the one the code makes.
The two Mapillary channels price off the shared z14 tile count, and the two KartaView channels off their bbox's swept circle count at 16/min (#238, #258).
Both census pairs price their walk off the **bbox**, never the sample count: a KartaView walk of Krabi is 64 estimated circles, not its 18,851 on-street samples, and the derived timeout is deliberately blind to whether the census is already cached — a walk that finds no reusable one must still be given time to fetch it.
The reason it is derived at all is that **a SIGKILLed child records no `api_usage`** — every `db.add_api_usage` call lives in the child, after the download returns — so a timeout that fires mid-run loses the whole spend from the daily ledger *and* burns one of the five `consecutive_failures` that nothing but a success resets.
**`_channel_estimate` reads the census cache and `estimate_requests` deliberately does not (#290).**
The first is what the budget gates (`est > budget`, `used + est > budget`) and the dry-run listing read, and it returns 0 when a probe finds a reusable census for that channel's provider — without which the cheapest channel of the night, a walk whose census the grid run bought minutes earlier, is exactly the one a nearly-spent budget defers.
The second also feeds the timeout derivations below, where a 0 would collapse a child's timeout onto the flat floor; and since a probe is marker-only, a hit is a strong hint rather than a promise — narrowed two ways, by comparing the marker's recorded store format against this build's and by probing with the window less `max_batch_hours`, so a format bump or a mid-batch expiry is a miss rather than a free-priced fetch — and the child may still fetch for real.
The `achieved_rate_fraction` differs per channel because what falls short of the configured rate differs: gsv uses **0.5** for the async engine's structural undershoot against its project quota (#304 removed most of that undershoot; 0.5 stays until prod nights re-measure it, since over-timing is the harmless direction), Mapillary **0.8** because its limiter is a hard ceiling the concurrent fetch tracks closely, and KartaView **0.5** because its walk is *serial*, so per-request latency cannot hide behind other requests in flight and at 16/min the 3.75 s interval is genuinely comparable to the latency of a 2,000-record page.
Derived values are then clamped to what is left of the batch deadline (never below `_MIN_CLAMPED_TIMEOUT_S`, 300 s), which is what bounds a metro KartaView sweep whose honest timeout exceeds `max_batch_hours` outright — acceptable only because #239's checkpoint turns that kill into a resume rather than a discarded night.
**For a NON-resumable channel (`gsv`, `gsv_streets`) the clamp now only shortens a child that fits (#373).**
Under a batch deadline, `_run_city_channels` compares the channel's derived need, `city_timeout_estimate_seconds`, against what is left, and a channel that needs more is deferred rather than launched clamped: no launch, no `record_attempt`, not attempted, and its `consecutive_failures` are untouched.
Only a city whose EVERY channel deferred costs no city-cap slot (`attempted == 0`); a city with one channel launched and another deferred takes its slot as usual.
A clamped launch of the same child would have been SIGKILLed at the deadline, recorded as a failure, alerted on at the default `failure_threshold` of 1, and its ledger write lost — a routine outcome once #372 let nights end on the deadline rather than the city cap.
**The predictor is the estimate, never the floored timeout.**
The estimate is the derivation above with the headroom and the fixed slack but without the 180-min floor, and `city_timeout_seconds` is pinned to be exactly `clamp(max(floor, estimate))`, so the two cannot drift.
The floor is a minimum a child is *given*, not what it *needs*: predicting with it would defer every gsv city for the last three hours of every night while a median one finishes in minutes.
The estimate is still a padded upper bound, so it defers some children that would have finished, and that is the right trade: a deferred grid run costs nothing (it stays due and leads tomorrow), a kill costs a failure and an alert.
Two deferrals are not free, and each is handled rather than denied:

- **A deferred FIRST grid channel defers the whole city.** When `providers[0]` (normally `gsv`) is a grid run and defers, every other channel of that city defers with it, each counted and logged once ("deferring the rest of this city so its snapshots stay paired").
  Run alone, the city's Mapillary census and walks would land tonight while the grid lands on a later date, un-pairing its snapshots — the city is the join point — and they would add per-IP Mapillary volume on a night that bought no grid run.
  A resumable first channel never deadline-defers, so the rule never fires for one.
  Nor does it when the first channel is a WALK — a city excluded from gsv can lead with `gsv_streets` — since a walk has no grid to pair with, and dragging the city's censuses along would only cost them a night: only the walk defers.
- **A walk deferred AFTER its grid landed is STRANDED by the deadline.** The grid success moved the city off the gsv-due list for ~83 days, exactly as a host stranding does (#341), so the walk is recorded on the breaker's stranded set with the reason "the batch deadline": the `Done:` line counts it (`N city(ies) STRANDED un-walked (K by the batch deadline)`), and its own WARNING log line carries the by-name `run-due --provider gsv_streets --city …` recovery command.
  A deadline stranding alone does **not** make the night unhealthy: once nights end on the deadline the last city of most nights can strand a walk, so alerting on it would be nightly noise, and the walk is still reachable through the bounded opt-in hoist.
  The command therefore lives in the scheduler log — and in the alert's log tail, and beside the host strandings in the STRANDED paragraph (marked `the batch deadline`), whenever the night is unhealthy for another reason.
  The #380 end-of-night retry never re-launches a deadline stranding, and it prices every other non-resumable walk against the remainder BEFORE its call: one that no longer fits "stays stranded — the deadline" and keeps its original entry, never counted as a deadline deferral too.

An estimate of None (pacing disabled, or a channel with no derivation) never defers, since "unknowable" is not "too long".
When the first channel launched, every later channel is judged on its own estimate — walks key on the frozen network, not on the grid run.
The resumable channels never reach this gate: `_sweep_launch_plan` already sizes their cap to the same clock, so they pause rather than being killed.
That is true of the gate, not of the counter: the whole-city rule counts a deferred grid run's resumable siblings in `deadline_deferred` too.
A need beyond the whole `max_batch_hours` window can never fit any night, so it logs a WARNING naming the remedies (shrink the grid, raise `max_batch_hours`, run it manually) instead of the INFO line; nothing tracks repeated deferrals across nights.
The gate reuses its estimate for the child's timeout (`_clamp_timeout`), so a launched channel reads the catalog for its derivation once.
Under `_MIN_PACED_LAUNCH_S` (the 600 s fixed slack) left, `_run_city_loop` stops starting cities with the deadline stop reason: every paced estimate is the fixed slack plus its pacing, so no non-resumable channel could fit and no resumable one could afford a request, and walking the rest of the due list would only log a line per channel and count floor-skips as budget deferrals.
The #380 retry pass gates on the same constant — before its start line, before its wait, and before each walk — or it would start inside the same 300–600 s dead zone, spend a real Overpass re-check and floor-skip a resumable walk into "deferred for budget".
The `Done:` line counts the deferrals per channel (`N channel(s) deferred for the deadline (gsv 3, gsv_streets 2)`), apart from both the budget and the sibling-sweep deferral, and a deferral alone does not make a night unhealthy.
**But a kill is a resume of the WORK and not of the SCHEDULE, and that is where the acceptance runs out.**
A *deliberate* pause exits `SWEEP_INCOMPLETE_EXIT_CODE` (83) and is amnestied in `_run_city_channels` beside the blocked- and busy-host conditions, so it can repeat indefinitely; a SIGKILL has no exit code, nothing can tell one that checkpointed progress from one that made none, and it counts a `consecutive_failure` that only a success resets — so five clamped nights quarantine the city for a 90-day cycle.
A metro sweep that cannot finish inside five nights therefore needs #248's per-(city, provider) dueness, not a larger timeout constant.
That budget of five is survivable only if the five nights are **consecutive**, so the checkpointed progress accumulates — and consecutive is what `_collect_due`'s hoist buys.
Note which of the two unfinished-sweep arms a nightly batch takes, because since #273 a HEALTHY sweep takes the **pause**, not the SIGKILL.
`_run_city_channels` hands **both** KartaView channels a request cap as `--kartaview-max-requests`, and the cap is sized against the wall clock as well as the ledger: `min(budget − used, what the child's own timeout can pace)`.
The second term is `_sweep_requests_within_timeout`, the inverse of `_kartaview_timeout_seconds` and read from the same rate, `_SWEEP_ACHIEVED_RATE_FRACTION` and `_TIMEOUT_FIXED_SLACK_S`, so the two cannot drift apart.
The remainder alone was not enough, and the arithmetic says why — with the caveat that **which of the two ceilings binds moves with the config**, which is why the cap is a `min` rather than the term that was smaller when #273 was written.
At the 10 h batch it was written against, prod's 16/min paced ~9,600 requests against a 10,000 budget, so a fresh night's remainder was **unreachable** outright and a city costing more than its timeout affords was still killed before the cap could bind; at the 12 h batch (raised 2026-09-02) the same rate paces ~11,520 and the budget is the smaller term instead.
A sweep that reaches its cap stops itself deliberately: exit 83, amnestied, no `consecutive_failure`, no city-cap slot, and the spend still reaches the ledger because the child returns.
What bounds a paused city is therefore not the five failures but `CHECKPOINT_MAX_AGE_S` — seven days from the checkpoint's **first** commit, after which its rows would be spliced into a snapshot dated today and it is discarded.
A child running **slower** than the assumed `rate × _SWEEP_ACHIEVED_RATE_FRACTION` is the one overrun a request cap cannot bound, and since #344 a clock inside the child bounds it.
Every resumable launch is also handed `--*-max-seconds`, set to `timeout_s − _CRAWL_CLOCK_MARGIN_S` (600 s, the same number as `_TIMEOUT_FIXED_SLACK_S`), from one helper at all six launch sites (`_crawl_clock_args`, the twin of `_request_cap_args`).
The child measures it from its own process start and checks it at the same tile/cell boundary as the cap, so a slow crawl pauses itself with exit 83 — checkpointed, ledgered, amnestied — exactly as a capped one does.
The two ceilings compose: whichever is reached first pauses the crawl, the child's pause line names which (`stopped by its request cap` / `stopped by its wall-clock budget`), and the scheduler carries that phrase into the paused child's reason.
**The SIGKILL arm is still reachable, by three routes, and none of them is a slow crawl:**

- a non-resumable channel (`gsv`, `gsv_streets`), which is never handed a clock;
- a crawl that completes inside its budget and then overruns in its **finalize tail** — grid assignment, the CSV write, the walk's join, stats — because the clock is checked only when a unit is admitted, never after the last one;
- a launch whose timeout is at or under the 600 s margin, which gets no clock flag at all (the child's `positive_int` would refuse it): the `est == 0` cached-census launch, and an unpaced channel whose `affordable` is None, neither of which the launch floor skips.

In-flight work at the moment the clock trips is not a fourth route in practice, and each crawler bounds it its own way.
A tile census bounds each in-flight tile's retry chain at `_TILE_MAX_TIME_S` (120 s).
The KartaView sweep has no tile timer, but it is serial and asks the clock before every probe and every page, so what is in flight is one probe: at most `DEFAULT_BACKPRESSURE_RETRIES + 1` = 4 attempts × (the 60 s `DEFAULT_REQUEST_TIMEOUT_S` + ~3.75 s of pacing at 16/min) ≈ 4–5 min.
Both residues sit inside the 600 s margin.
The SIGKILL arm does count a `consecutive_failure` and does consume a slot, which is why the five-night bound still exists for it and why the hoist has to put tomorrow's retry in the *first* slot rather than merely in the list.
The union of the per-channel due lists is ordered by first appearance, so `gsv` (rank 0) dictates city order; a city whose `gsv` run succeeded but whose sweep paused sits at the tail of ~949 cities and is truncated by `max_cities_per_day`, returning months later rather than tomorrow.
The hoist moves a city to the head of the slate when **every** channel it is due on is opt-in — `all`, not `any`, so a city due on `gsv` too keeps its exact union position and `gsv`'s stalest-first ordering is strictly untouched.
It reorders the **city list only**, never the union loop, because `providers_for_city` is passed straight to `_run_city_channels` where `pending = list(providers)` *is* the launch order.

**The tail — aggregate, streetwalk manifest, catalog backup, publish — is what makes a night visible, and it only runs if the city loop returns**, so every way of ending the loop goes through `_run_city_loop`,
which always returns counters instead of propagating: a `[schedule].max_batch_hours` deadline (12 h) stops *starting* cities, defers a non-resumable channel whose derived need exceeds what is left (#373), and clamps every other in-flight child's timeout to it,
a SIGTERM handler turns systemd's stop into a wind-down request checked between cities *and between a city's channels*, and an unexpected exception is logged, published anyway, and then reported as an unhealthy night (nonzero exit + alert, so publishing can't hide a bug).
**The tail also prunes the shared census cache** (`prune_census_cache`, #290), beside the backup and the publish and with the same best-effort posture — it swallows its own filesystem errors, because the prune is housekeeping and the publish and the alert come after it.
That prune is the only thing bounding the cache's size: an entry is written for every census a night fetches and is not overwritten until that city comes round again, ~80 days later.
**The tail also records the night's cgroup memory peak** (`cgroup_memory.describe_cgroup_memory`, #305) — read after the index rebuilds, because on a big-census night the tail and not the city loop sets the peak (#157), and quoted against `MemoryHigh` rather than `MemoryMax`, since crossing the soft brake produces a night that is merely slow while crossing the hard one produces an OOM kill an operator can read.
It is both logged and appended to the summary, which is not redundancy: the `Done: …` line is emitted before `_finish_batch` runs, so a summary-only append reaches the `[alerts]` email and never the scheduler log.
`grep 'cgroup peak' logs/streetscape_scheduler.log` is therefore the retrospective form; `systemctl show -p MemoryPeak` answers only while the current cgroup exists and resets on the next start.
The line carries `memory.events`'s `high` counter beside the percentage, because `memory.peak` is the high-water mark of `memory.current` — page cache and kernel memory included — and so is a proxy for throttling rather than evidence of it; `throttled 0 times` at 94% says the brake never engaged, and a non-zero count at 47% says it did.
It is read **before** the tail catalog backup and the publish rsync and `memory.peak` is monotonic, so neither of those can ever appear in the number — structural, since `summary` must be complete before `_publish` receives it, and named here rather than assumed away.
Same best-effort posture as everything else here — an unreadable or absent cgroup adds nothing at all, and never `0`, which would read as a night with enormous headroom.
**The tail's own index rebuilds carry that same posture** — each of the three (`generate_aggregate_v2`, `generate_streetwalk_manifest`, `generate_driving_plan_summary`) runs through `_tail_artifact`, which reports a crash (alert naming the index + nonzero exit) instead of propagating it, so a broken index can't cost the catalog backup and the publish that follow.
Safe because all three write via `_write_json_gz_atomic`, leaving the *previous* good file in place: the publish ships a stale-but-valid index, never a truncated one.
This gap was real, not theoretical — on 2026-08-17 a manual catch-up piped to a reader that had gone away (`run-due ... | tail -40`) collected 10/10 cities and published none of them, because `tqdm`'s `status_printer` flushes the **raw** `sys.stderr` outside its own `DisableOnWriteError` guard and the resulting `BrokenPipeError` took out the whole tail.
**A dead output pipe is therefore treated as an ordinary condition in five separate places, because any one of them alone still loses the night.** (1) **Every progress bar in the repo goes through `progress()`** (`progress.py`) and never a bare `tqdm`
— pinned by a source-inspection test, since seven hand-edited call sites is a rule for humans and the eighth reintroduces the bug.
`disable=None` is *not* the fix and must not be restored as a simplification: tqdm decides using `file` alone (default `sys.stderr`) but its `status_printer` then flushes **both** raw streams
— and `DisableOnWriteError.__eq__` proxies to the wrapped stream, so that guard's membership test is True
— meaning a live stderr with a dead stdout (`run-due | head`, the incident command *without* `2>&1`) leaves the bar enabled and raises anyway.
`progress()` draws a bar only when **both** streams are TTYs.
(2) Because that makes bars *always* off under the scheduler (a child's stdout is a per-attempt log file), `progress()` takes a `logger=` and emits one progress line a minute instead
— the three long collectors (`download_gsv`, `download_gsv_history`, `download_mapillary`) pass it, since #157's "printed nothing after `Decoded …`" diagnosis is only possible when a healthy run *does* print, and silence makes "hung" and "slow" indistinguishable after a SIGKILL.
Fast work (grid generation, the aggregate) omits it.
(3) **`_publish` redirects its child's stdio** to `logs/publish_{date}.log` rather than inheriting it.
Python ignores SIGPIPE only for *itself* — `subprocess` restores `SIG_DFL` in children
— so an inherited dead fd 1 killed `sync_data_to_server.sh` (`set -euo pipefail`) on its first echo, before any rsync: the tail would run, reach the publish, and still ship nothing.
(4) **`main()` exits through `_exit()`**, which flushes the std streams itself and points a broken fd at `/dev/null`, because CPython otherwise **replaces the process exit status with 120** when finalization's flush fails
— silently clobbering the whole 0/1/64/75/76/79/80 vocabulary on any piped run, `setup_logging`'s `StreamHandler(sys.stdout)` guaranteeing there is buffered data to fail on.
`cmd_regenerate` — the recovery `CLAUDE.md`'s commands cheatsheet prescribes for a stale index
— gets the same treatment: its rebuilds go through `_tail_artifact` and its prints through `_emit`, because it used to `print()` *before* publishing and so aborted the recovery before it recovered anything.
(5) **A `BrokenPipeError` out of a `run-due --dry-run` returns 1 without alerting**, because on 2026-09-02 at 21:10 one emailed a `run-due CRASHED` alert for the dry-run preview's own `print()` when the SSH session reading it went away.
A preview collects nothing, so nothing was lost; the nightly cannot reach this branch at all, its stdout being a file (`StandardOutput=append:`) with no reader to disappear.
**Scoped to `--dry-run`, and that scope is the load-bearing half**: on a real night the exceptions that reach `main()`'s handler are precisely the ones `_tail_artifact` did *not* already convert into a scoped alert — the pre-flight backup, the driving-plan fetch, the tail backup, `_finish_batch`'s own body — so a broken pipe there is a night that collected and did not publish, which is the 2026-08-17 incident again and reaches an operator only by email.
The supported manual catch-up (`run-due --provider mapillary --limit 5` — small N since the 2026-09-09 staging rule, not the old `--limit 40`) runs over SSH too, so the dropped-session trigger is not a dry-run-only shape.
It is one `except Exception` dispatching on the type rather than two clauses, because a `raise` from inside an `except BrokenPipeError` is *not* caught by a sibling `except Exception` and would drop the alert in exactly the case it exists to keep.
The branch calls `_neutralize_broken_streams()` (shared with `_exit`) before it logs: logging to a broken stdout does not raise, but `handleError` writes `--- Logging error ---` and the whole traceback to stderr, which under `| head` is still a live terminal — so without it the one-line warning arrives with the traceback it replaces stapled to it.
Still: drive manual batches into a file (`>> logs/x.log 2>&1`), not a pipe.
**`systemctl stop` is a real wind-down as of issue #206, and getting there took three fixes, not the two the issue named.** It used to be a hard kill: measured 2026-08-13, `systemctl stop` → SIGTERM at 06:23:28 → SIGKILL at 06:24:58 → **no tail**, the night's collected runs left unpublished until a manual `regenerate-aggregate --publish`.
**(1)** The unit now sets `TimeoutStopSec=30min`; without it systemd applies a **90-second** default that expires long before the tail can run.
Its size is pinned by a test against the **sum** of the tail's two large known terms
— `catalog_backup.BACKUP_TIMEOUT_S` (600 s) plus the measured aggregate+manifest rebuild (`_MEASURED_TAIL_AGGREGATE_S`, 435 s on the 19-city night of 2026-08-18)
— and capped below `max_batch_hours`, since a stop timeout above the batch's own deadline makes `systemctl stop` and host shutdown hang.
The sum, not the larger term: the aggregate runs *before* the backup and neither substitutes for the other, so a bound of `> BACKUP_TIMEOUT_S` alone accepted `11min`, which the very sentence justifying it (the issue's suggested `10min` "could not have reached the publish") rules out.
Both figures are named constants because they are measurements, and re-sizing has to argue from a number with a date on it: `_publish` logs `Published in N.N s` precisely so the rsync
— the tail's largest and, until #206, only unmeasured component — is one of them.
**It is now bounded too (issue #230): `PUBLISH_TIMEOUT_S` is 600 s**, and the test's floor is therefore the sum of all three — 600 + 435 + 600 = 1635 s, under the 1800 s directive.
That sum, not the bare inequality #230 suggested, is the load-bearing form: the publish runs *after* the backup and the aggregate, so a bound that merely sits below `TimeoutStopSec` on its own still gets SIGKILLed partway through, i.e. the pre-#230 outcome reached one step later.
Read as a constraint from the publish side it says `PUBLISH_TIMEOUT_S` cannot pass ~765 s without the directive moving first.
The value is measured, not guessed — 16 nights of prod logs put a healthy publish at p50 12.1 s, p95 24.3 s, **max 25.5 s**, and those are *upper* bounds, since pre-#229 the only available interval (`Publishing via` → the next log line) also contains the alert's SMTP send;
the rsync's tree walk over the 7,409 published files (7,416 rsync candidates) is 0.138–2.303 s of it depending on NFS dentry-cache state, so what the clock actually buys is transfer, and the bound has to grow with published *volume* rather than file count (`docs/experiments/publish-duration.md`, `scripts/publish_duration_analyze.py`).
600 s is ~23× that max and deliberately the same number as `BACKUP_TIMEOUT_S`.
**The kill has to reach the process GROUP, and `subprocess.run` cannot**: `cmd` is `["bash", sync_data_to_server.sh]` and that script runs rsync as an ordinary child with echoes after it (no implicit `exec`), so `run`'s timeout path — `Popen.kill()` → `os.kill(self.pid)`
— reaches only the **shell**, leaving the wedged rsync reparented and still holding the transport, still appending to the per-day publish log after `_tail_lines` read it, and still live when a later `regenerate-aggregate --publish` appends to that same file and starts a second rsync into the same docroot.
`_run_publish_child` therefore uses `Popen(start_new_session=True)` + `os.killpg`, and kills the group on `KeyboardInterrupt` too, since a child in its own session no longer receives the terminal's Ctrl-C (it does *not* leave the cgroup, so #206's `systemctl stop` still reaches it).
**One case that still does not cover, stated rather than implied**: a child the *kernel* will not kill
— a `--local` child stuck in an uninterruptible NFS RPC defers SIGKILL until the mount answers, exactly as it would defer systemd's, and no userspace bound ends that process.
What this code can do is refuse to *wait* on it (`_PUBLISH_REAP_GRACE_S`, 30 s, whose expiry is logged) rather than inheriting `subprocess.run`'s unbounded post-kill `wait()`;
the failure line prints the bound and the real elapsed as two numbers precisely so that deferral is visible somewhere.
A timeout is reported as an ordinary publish failure (logged, alerted, nonzero), never raised, per #167.
**And the failure text finally reaches the email (issue #218):** `_publish` copies the publish log's tail into the *scheduler* log the way `_run_collection_subprocess` does for a failed child, because the nightly path passes `alert_on_failure=False` so the batch tail can send one combined email
— and that email quotes `_recent_log_tail` and nothing else, so while the tail lived only in `_publish`'s own alert, every night that failed to publish reported a bare status and left the rsync error in a file on a host nobody reads.
That paste is why the batch email reads `_BATCH_LOG_TAIL_LINES` (120) rather than the 40-line default: a failed publish contributes `_CHILD_LOG_TAIL_LINES + 2` lines and is the **last** thing a night writes, so at 40 it took 27 of them and evicted the report of which cities failed and which host refused us — the fix eating the context it exists to be read beside.
The window is sized against what gets *pasted* into the log, not against the log's own narrative.
**(2)** The stop flag is checked at **both** levels.
It was checked only in `_run_city_loop`'s outer `for city in due`, so a stop still launched every remaining channel of the in-flight city
— with Mapillary enabled, firing its channels into a live tile block, i.e. the exact thing the operator was stopping to prevent.
It is now threaded into `_run_city_channels` as a **required, no-default** `stop_requested: threading.Event | None` (the `batch_deadline` precedent: a caller that silently inherited "nothing can stop this" would look correct until someone typed `stop`), checked as the first statement of the per-channel loop, and `break`s rather than `continue`s
— every other guard there is a property of one *channel*, so a later one can answer differently;
a stop is a property of the *process*, so none can.
(Since #240 that same check is the lane scheduler's **submit gate**, with the identical argument: nothing further is launched, while a child already in flight is left to finish and credited, because it has been paid for either way.)
`assess-city` passes `stop_requested=None` for the same reason it passes `batch_deadline=None`.
The loop also re-checks after `_run_city_channels` returns, which matters twice: the inter-city sleep would otherwise burn its whole interval out of the stop window (PEP 475 *resumes* `time.sleep` after the handler runs rather than returning early, so the flag is set and ignored for the whole interval),
and on the **last** due city there is no next iteration at all, so the night would have summarized as complete while that city's remaining channels went uncollected.
That was a full minute of the stop window until #306 cut `sleep_between_cities_s` from 60 s to 5 s; the check is not sized to the constant and must survive it moving back.
**(3)** The child killed by our own stop is **not** recorded as the city's failure.
`KillMode` defaults to control-group, so the SIGTERM reaches the whole cgroup and the in-flight child returns `-15`
— a code in neither `HOST_BY_EXIT_CODE` nor `HOST_BY_BUSY_EXIT_CODE`, so it read as an ordinary collection failure.
This defect was invisible until (1) and (2) landed, because the SIGKILL destroyed the tail before the alert could be sent;
fixing them without it would have traded a silent failure for a **false alarm on every deliberate stop**
— `record_attempt(success=False)` burning one of five `consecutive_failures` that nothing but a success resets, and `attempted > succeeded` alerting (prod `failure_threshold = 1`) and exiting nonzero.
The check sits *after* orphan salvage (so anything the child finished is still cataloged) and *before* `attempted += 1` (so a channel we killed is not counted at all), mirroring how the blocked/busy/argv-rejected branches `continue` before that same line.
A stopped night is therefore benign: it publishes, exits 0, and the declined channels keep their cadence and lead the next batch's queue.
**Both stop exits name the channels they declined, via the shared `_log_stop_declined`**
— and that sharing is the point, because the exit that *reads* like the main path is the one an operator almost never hits: the cgroup SIGTERM kills the in-flight child first, so the loop leaves through (3) and never returns to (2)'s check.
While the message lived only at (2), the complete operator-visible record of a stopped four-channel city was one `child was killed by the stop signal` line, with the three Mapillary/streets channels it declined named nowhere
— losing exactly the information the operator typed `stop` to obtain.
(3) passed `providers[i + 1:]`, since its own channel *was* started and is reported on its own line; since #240 both exits converge on the set of channels still un-launched when the city drains, so there is one call site and no wording to keep in step.
The helper is silent on an empty list either way, so a stop landing on a city's last channel can't claim it skipped work that never existed.
Note also what a stop does **not** suppress: the three unconditional alerts (host refused, backup failed, driving-plan fetch failed) still fire and still exit nonzero, so a `host(s) UNAVAILABLE` email after a deliberate stop is the wind-down working.
**The installed unit on makelab2 is a copy, not a symlink**, so all of this stays inert until someone re-copies it and runs `daemon-reload`
— verify with `systemctl --user show streetscape-tracker.service -p TimeoutStopUSec`, which must read `30min` rather than `1min 30s`.
One known gap, deliberately left: `_finish_batch` runs *outside* the `_stop_on_sigterm` context, so a **second** `systemctl stop` during the tail kills the publish with the default handler. systemd sends SIGTERM once and then SIGKILL, so this is not the deployed failure mode — but do not type `stop` twice.
This deadline must stay **below the unit's `TimeoutStartSec` (14 h)** — a test asserts the two files agree
— because reaching the systemd limit means a SIGKILL mid-loop, which is exactly how 2026-07-29 collected most of a night and published none of it (#167).
A child that exits with a `HOST_EXIT_CODES` status trips the per-IP **host breaker** (`HostBreaker`, and see the host-lock section of [`provider-access.md`](provider-access.md)): that host's channels are skipped, no city is marked failed, and the night alerts unconditionally and exits nonzero while still publishing.
**The latch is no longer all-night for Overpass (issue #341).**
`blocking(hosts)` — the one call the launch pass makes — re-checks a latched host that has a positive reset test in `HOST_RECHECKS` (Overpass only), at least 45 min after the trip or the last failed re-check and at most four times a night, and a host that answers is un-latched from that launch on (for Overpass, "answers" means a tiny `/api/interpreter` query executed, never a `/status` read, #356); the set itself stays monotone, so `bool(blocked_hosts)` still means "refused tonight" for the alert and the exit status.
A walk whose GraphML is already frozen for the channel's `network_type` is launched despite a latched Overpass, because it never contacts it.
**Before a walk exits 76 at all, its fetch rides out the refusal for up to the `[overpass]` retry window** (#357; ~7.5 min by default, 30 s floor between attempts, inside the 900 s fetch deadline) — so a minutes-scale flap no longer trips the breaker, and each trip that does happen costs that window of wall clock in the walk's lane, at most `1 + HOST_RECHECKS_PER_NIGHT` times a night; see "(6)" in [`provider-access.md`](provider-access.md)'s Overpass section.
**That window is shortened per child to fit the timeout it will be SIGKILLed at** — `_run_one_city` derives the timeout before the argv and passes it to `_street_collect_cmd`, which calls `policy_for_child_timeout` — because the deadline clamp floors a late city at `_MIN_CLAMPED_TIMEOUT_S` (300 s), under the ~450 s a refusal costs, and a SIGKILL records no exit code for the breaker to read while still counting a `consecutive_failure`.
**Stranding is recorded after the city drains**: a walk an unavailable host cost the city — the refused child itself, a breaker skip, a child that found the host busy with another local process (exit 80, most often our own daytime pre-freeze pass overrunning into the timer), or a child whose argv our own CLI rejected (exit 2, #359) —
whose grid sibling (`STREET_CHANNELS[walk]`) succeeded tonight leaves the city with a grid run and no walk and not due on the grid channel for ~83 days, so it is counted on the `Done:` line (`N city(ies) STRANDED un-walked` — not "by the breaker", since a busy host and a rejected argv strand a city too), named in the alert beside one pasteable `run-due --provider <walks> --city <id>...` per exact channel set (#362), and logged as a `STRANDED` warning per lost walk.
Decided after the drain rather than at the skip because with lanes the sibling may still be in flight at skip time.
**An argv our own CLI rejected is a fourth exit-code family, `ARGV_REJECTED_EXIT_CODE = 2` (issue #359)** — argparse's number, inherited rather than allocated.
The scheduler builds every child argv itself, so under `run-due` a child exit 2 never means an operator mistyped: it means the config and the CLI disagree.
`_run_collection_subprocess` names it and quotes the argv in the outcome's reason (the child log is appended across attempts, so its argv header can fall outside the quoted tail), and `_run_city_channels` amnesties it beside blocked, busy and crawl-incomplete — no `record_attempt`, so it never becomes a `consecutive_failure` and the city stays due.
Before #359 five such nights quarantined a city for the rest of its cycle, with no alert telling it from a city that was not due.
It is classified on the LAUNCHER side rather than by a code each child emits, so every parser — present and future, and the interpreter's own exit 2 for a missing script — is covered with no per-child code to forget.
It is deliberately **not** a breaker: per-city argv differs (the connection share, the request cap), so the next city's launch is still asked.
The per-channel `rejected_argv` counter reaches the `Done:` line (`N launch(es) REJECTED by our own CLI (<channels>)`) and `_finish_batch`, which alerts unconditionally and exits nonzero under its own subject part, because the operator's next move is a config edit rather than waiting or hunting a process.
The "two settings that disagree" are named by the parser itself, and the alert quotes it per rejection rather than pointing into the recent-log tail, which on a full night has scrolled past the `exited 2` lines: `rejected_argv` is an `ArgvRejections` — a per-channel `Counter` that also keeps one `(city_id, provider, reason)` per launch — and `_rejected_argv_alert_note` lists the first `_ARGV_REJECTIONS_LISTED` (10) of them, like the stranded-city list.
Each reason carries the redacted argv, the parser's last `prog: error:` line (lifted from the child-log tail by `_run_collection_subprocess`), and the child log's name.
A rejected WALK whose grid sibling succeeded is recorded as STRANDED exactly like a busy skip, since the consequence is identical.
`assess-city` counts it too: its summary says `N channel(s) REJECTED by our own CLI` and the run exits 1, since a rejected channel records no attempt and would otherwise score 1/1.
The one rejection the scheduler could plausibly provoke, a `connection_limit` above what the GSV engine can use, is now a clamp with a stderr warning in `cli.py` rather than a `parser.error`.
Since #304 that bound is `download_gsv.max_requests_in_flight(batch_size)`, i.e. `PIPELINE_DEPTH × batch_size` (400 in prod): the engine keeps that many requests in flight at most, bounded again by a `connection_limit`-sized semaphore, so sockets past it never get work (before #304 it was one batch, `batch_size`).
**Stranded walks are retried at the end of the same night (#380).**
After the `for city in due` loop, and inside the same `try` so the #167 guard covers it, `_retry_stranded_walks` launches each stranded (city, walk) again, one walk per `_run_city_channels` call and serially, through the night's own breaker, `batch_deadline` and stop flag — so a `--city`, `--limit` or `--provider` run inherits it, and a stopped or out-of-time night passes straight through.
The breaker's re-check is the only probe: a walk it still skips, or whose child is refused again, gets ONE more try, and there is no retry budget beside `HOST_RECHECKS_PER_NIGHT`.
Before that try the pass waits at most once (`_wait_out_recheck_cooldown`, an `Event.wait`, so a SIGTERM ends it at once), and only for walks whose latched hosts are ALL re-checkable with a re-check left — a walk also held by a host that is never re-checked (the tile CDN, KartaView, Panoramax) cannot launch however long the pass waits, so it is dropped rather than made to cost the night 45 min and an Overpass re-check (PR #384 review).
The wait runs until the breaker's NEXT scheduled re-check (`HostBreaker.seconds_until_recheck`), not a flat cooldown, and the deadline gate uses that same figure plus `_MIN_PACED_LAUNCH_S` (600 s, #373); a walk whose hosts all cleared in the meantime is retried without waiting.
The pass's re-asks are not launches: `skipped_launches` and `skipped[host]` are restored around each one, so "launch(es) skipped while latched" still counts one channel of one city.
A stop that ends the pass is reported like any other — a SIGTERM that lands during the last retried child, or the pass's own deadline exit (`batch deadline reached (… h) during the end-of-night retry`) — and joins the loop's reason when both stopped something (`city cap reached (N); received SIGTERM`).
A busy strand (exit 80) is retried through the same host lock.
**An argv-rejected strand (exit 2, #359) is never retried**: the rejection is a config/CLI contradiction that a second launch would repeat exactly, and a second `rejected_argv.record()` would double the `REJECTED` count and name the city twice in the alert. It is recorded apart (`HostBreaker.argv_stranded`) and stays STRANDED, named once.
A walk whose network was frozen is never stranded *by Overpass*, but a frozen census walk (`mapillary_streets`, `kartaview_streets`, `panoramax_streets`) refused by its census host is stranded, and retried, like any other.
A walk that lands leaves `stranded` and is counted in `HostBreaker.stranded_recovered` (`N stranded walk(s) recovered by the end-of-night retry` on the `Done:` line and in the alert); anything else **keeps the original entry**, which is never re-derived, because `_run_city_channels` strands only when the grid sibling succeeded in the same call and the retry's call carries the walk alone.
So the `Done:` count, the subject and the named cities are what is STILL stranded.
**`busy_hosts` follows the same rule**: a walk stranded by a busy lock (`HostBreaker.busy_stranded`) that the pass lands is taken back out of it and counted in `busy_recovered`, so the busy paragraph (whose "they stay due" would otherwise be false) and the `SKIPPED (host busy)` subject report only busy skips still outstanding; one busy again at the retry stays counted once, not twice.
**A recovered busy strand still makes the night unhealthy**, exactly as a refusal that recovered does (both cost launches, and the lock's other holder — most often the daytime pre-freeze pass overrunning — is still worth finding): the subject says `N host(s) BUSY then recovered`, mirroring `REFUSED then recovered`, the body names the host and the recovered count, and the night exits nonzero.
A refusal whose stranded walks the pass all landed likewise still alerts `REFUSED then recovered` and exits nonzero, because the breaker's host set is monotone.

**Night-length measurements include the pass**: on a night that strands a walk, the `Done:` line's elapsed hours (and so `scripts/night_length_analyze.py`'s `hours`) now include the retry pass and its at most one re-check wait, so such nights are longer by design, not from slower collection.
Retries count in `attempted`/`succeeded`, never in `processed`, and each carries tonight's UTC `--run-date` — a 02:15 Pacific start plus the 12 h `max_batch_hours` ends at 14:15 Pacific, before the UTC rollover in both PDT and PST; never backdate it.
**Pairing by date is best-effort; cost reuse holds for 7 days.**
Nothing in `json_summarizer.py`, `analysis.py` or `www/js` joins a walk to its grid run by date: what a shared date buys is the #290 census reuse, which already tolerates `CENSUS_REUSE_MAX_AGE_S` (= `CHECKPOINT_MAX_AGE_S`, 7 days), so a census walk within a week of its grid run is still free, while `gsv_streets` pays per sample either way.
A refusal that recovered still makes the night unhealthy, with a subject that says `REFUSED then recovered` rather than `UNAVAILABLE`, since the operator's next move differs.
The daytime `scripts/prefreeze_street_networks.py` is the prevention: it predicts the night's walk slate through `_collect_due` (hoist and refresh reserve included, for tomorrow's UTC date), freezes the cold networks serially and paced, stops on a host condition with that host's exit code, and refuses to run beside an in-flight `run-due` unless forced.
It runs daily at 15:00 Pacific from `streetscape-prefreeze.timer` (#355), which is not `Persistent`, so a boot-time catch-up can never land beside the 02:00 batch.
Because makelab1 is shared, a `[resource_guard]` pre-flight (pure `plan_connection_limit`, Linux `/proc` read) lowers each run's `--connection-limit` when host load/free-RAM are tight — on top of the systemd unit's static CPU/RAM caps.

## Channel order, and the four rationales it did not have (issues #240, #238)

**The rule is "most expensive first, EXCEPT where truncation is cheapest to absorb."**
`SchedulerConfig.enabled_providers` returns a fixed rank — gsv, `gsv_streets`, mapillary, `mapillary_streets`, kartaview — and the docstring there states the rule; this section holds the mechanism and the history, because the docstring was the wrong size for it and because being the collision point for every branch that touched the ordering is how the wrong versions kept getting copied.

**The mechanism is the deadline clamp, and it is the only wall-clock lever ordering has.**
`remaining_s` is read fresh at every launch — one `time.monotonic()` per *launched* channel (or deadline-deferred one, #373), in the launch pass — and `city_timeout_seconds` clamps the derived timeout down to it, floored at `_MIN_CLAMPED_TIMEOUT_S` (300 s).
A channel launched later therefore sees less of the batch deadline, and an expensive one launched late can have its timeout truncated to the floor and be SIGKILLed part-way, which costs its whole spend from the daily ledger (`db.add_api_usage` runs in the child, after the download returns).
Since #373 that kill is reachable only by a child running slower than its own derivation, or by one whose estimate is None (pacing disabled, so there is no derivation to defer on and it launches under the clamp as before): a non-resumable channel whose estimate exceeds the remainder is deferred instead of launched, and a resumable one is capped by `_sweep_launch_plan`.
So the channel needing the most wall-clock should start while the most of it remains.
`test_the_deadline_is_a_submit_gate_and_every_lane_child_gets_its_own_remaining_s` pins this, as a decreasing sequence in submit order.

**The rule inverts past one point, which is why kartaview ranks last rather than first.**
"Expensive first" holds only while no single channel is long enough to consume the deadline by itself.
One that *is* starves everything behind it — put it first and its siblings launch against what is left, down to the floor — so for that channel the question stops being which is most expensive and becomes which can best absorb being truncated.
A multi-hour KartaView sweep is that channel: last, exactly one channel eats the clamp, and it is the one #239 checkpoints, so a truncated sweep resumes instead of re-paying for the cells it already fetched.
Since #344 a truncated resumable child is not killed at all in the ordinary case — its clamped timeout also sizes its wall-clock budget, so it pauses itself with exit 83 before the SIGKILL.

**Since #290 the order also decides who FETCHES and who REUSES.**
`mapillary` (rank 2) launches before `mapillary_streets` (rank 3), so within a city the grid run pays for the shared z14 census and the walk reads it for zero requests; `kartaview` (4) and `kartaview_streets` (5) are the same pair over the radius sweep, wired in #258.
Measured on the first KartaView walk (Krabi, 2026-08-31): 87 sweep requests un-paired, against the 18,851 that same walk costs on `gsv_streets` at one request per on-street sample — and 0 on any night the grid run got there first.
That is a consequence of the existing ranking rather than a new constraint on it — reversing the pair would simply move which channel's ledger carries the spend, and `census_fetched_by` would record that faithfully either way — but it is why the two are ranked adjacently and why nothing should separate them.
Nothing else here has that (Mapillary's checkpoint is #256, and a truncated tile census re-spends against the per-IP ceiling — since #385 the 3,000-per-rolling-24-h host pool, which binds before 5,250, the mere sum of the two daily budgets; see docs/provider-access.md).
**Cheapest is not free, in two ways that both matter.**
No channel keeps its ledger row through a SIGKILL, whatever its provider.
And a SIGKILL still counts a `consecutive_failure` — only a *deliberate* pause (exit `SWEEP_INCOMPLETE_EXIT_CODE`) is amnestied — so the resumption that justifies this ranking is itself bounded at five nights.
For the six resumable channels the wall-clock stop (#344) turns a clamp that used to end in a SIGKILL into that deliberate pause; for the two GSV channels a clamp still ends in the kill.
Ranking picks who absorbs the truncation; it never makes it free.

**What order also decides**, both verified in the launch pass: which channels have **finished** when a wind-down stops the city, and which claim a lane first when a city has more channels than lanes.
Note *finished* and not *launched*: a SIGTERM is a submit gate (#206), but the unit's `KillMode` defaults to control-group, so a real `systemctl stop` takes the in-flight children with it — `_log_stop_declined` says so, and the amnesty branch exists because those children show up as `exited -15`.
Above one lane it is the **attempt** order rather than the launch order, because host affinity can defer a higher-ranked channel and let a lower-ranked one take the free slot.

**Four superseded rationales, and what each got wrong. Read these before writing a fifth.**
Every one was reasoned from prose adjacent to the docstring instead of from the code that prose describes — the launch pass, ~200 lines away the whole time — and each read as established long enough to be quoted elsewhere before anyone checked it.

- **"A city's channels share one night's budget, so the series that can exhaust a budget should claim it first."**
  There is no shared pot: `daily_request_budget` is per-`ProviderConfig` and `db.get_api_usage` is keyed by `(date, provider)`, so no ordering can let one channel claim anything ahead of another.
  Traced: the pre-#240 wording was "run back-to-back **within** one night's budget" — a claim about *timing*, true sequentially — and `9d20afe` reworded it to "**share**" because back-to-back had stopped being true under lanes, silently converting a timing claim into a shared-resource one while carrying the conclusion along unchanged.
- **"Lane occupancy": a long pole first would make the others queue behind it.**
  Above one lane it takes **one** lane while the others take the rest, so the queueing harm cannot occur — and as a wall-clock argument it points at rank **0**, since submitting last makes the city finish later.
- **"Deadline priority": rank a channel last and it is the first thing a truncated night drops.**
  The batch deadline is checked in `_run_city_loop`, **between cities**; the launch pass had no deadline gate at all — only the lane cap, the SIGTERM submit gate, host affinity and the budget guards.
  Once a city starts, every one of its channels is attempted whatever the order, so truncation does not operate at channel granularity.
  Since #373 the launch pass does have one, for non-resumable channels only, and it still does not make this rationale true: it defers a channel for its own NEED against the remainder, never for its rank.
- **"It can afford to absorb the clamp, because #239 checkpoints it."**
  True of the work and false of the schedule, until #238's review: `SWEEP_INCOMPLETE_EXIT_CODE` appeared nowhere in `scheduler.py`, so a checkpointed pause reached `record_attempt(success=False)` exactly like a crash, and `get_due_cities` filters on `consecutive_failures` with only a success resetting it.
  Absorbing truncation was not cheap, it was cheap*er*.
  Fixed by amnestying exit 83; the rank-4 *decision* survives, since one channel eating the clamp beats several.

The pattern is the finding, not any one of the four: reading the launch pass settled all of them in a single pass — no deadline gate, no shared ledger, a fresh clock read per launch — and that check was available from the start.

## Concurrent channel lanes (issue #240)

**A city's channels may run at once — but never two that need the same per-IP host, and the city loop itself stays sequential.**
`[schedule].max_concurrent_channels` (default **1**) is how many of one city's channels `_run_city_channels` will keep in flight.
This is the issue's **Shape A**: the loop over cities is unchanged, so a night's wall clock becomes the sum over cities of `max(channel)` instead of `sum(channel)`, and **paired snapshots survive** — every channel of a city still shares one run date, which is the property that makes its providers comparable at all.
**Shape B — a per-provider queue per lane — stays rejected**: lanes advance at different speeds, so the per-night city sets diverge and paired snapshots break structurally, every night, for every city.

The opt-in hoist (#248) is the one deliberate exception to Shape A's "the loop over cities is unchanged", and it carries a cost worth stating: a city that pauses is due tomorrow, hoists to index 0, runs first, and `city_timeout_seconds` clamps it to what is left of `max_batch_hours` — so it can take essentially the whole night, every night, for as long as it keeps pausing.
Today's `gsv`-first ordering is *accidentally* protecting the nightly slate from that, and the hoist removes the protection at the same moment it enables the channel that needs it.
The seed set is safe because Krabi and Yogyakarta are small, which is a property of the curated set rather than of the design; `enroll-city` prints each city's lattice estimate so keeping the set under one night is a decision rather than a discovery.
**The hoist is now BOUNDED (#282 landed): `[schedule].opt_in_cities_per_day` reserves a share of the city cap for opt-in-only cities, and at most that many are promoted.**
Reserved slots rather than an unbounded hoist, because the mechanism's success case and its starvation case are the same case at different N — "due only on the opt-in channel" is the *normal* steady state for an enrolled city, since `gsv` succeeds nightly and advances its clock while the opt-in channel's stays put.
Unset means a quarter of `max_cities_per_day`, floored at 1 (**5** at prod's cap of 20), and `scheduler._opt_in_reservation` is the single place that `None` becomes a number, so a config that sets the key and one that does not cannot disagree about what it means.
The resolution happens against the run's *effective* cap, so `--limit` scales the split with the cap it overrides — and an **explicit** value is scaled by the same ratio rather than merely clamped, because a bare `min(configured, max_cities)` saturates: at the `opt_in_cities_per_day = 5` the config comments show an operator uncommenting, `run-due --limit 4` would have clamped to 4 and handed the whole night to opt-in-only cities, reaching this key's own starvation case through the flag meant to narrow a run.
At `--limit == max_cities_per_day` the scaling is the identity, so a nightly run is unaffected.
Cities beyond the reservation are **not dropped** — they keep their union position and wait for a later night, which makes the key the *rate* a widening proceeds at: the enrolled set divided by it is how many nights a full pass takes.
Because they keep that position, "waiting" is measured against the **city cap**, not against the reservation: an unpromoted city inside `max_cities` still collects tonight, so on `run-due --provider kartaview --limit 40` with 40 due cities all 40 run and none wait.
**The reservation rotates at two levels: across strandedness kinds, and inside the opt-in-only kind across opt-in CHANNELS (#348).**
The kinds are excluded from rank 0 (#301), due only on opt-in channels (#248), and transiently not due on rank 0; after the live-checkpoint take below, the reservation round-robins across them.
That rule was not enough one level down.
Inside the opt-in-only kind the union is still first appearance over `enabled_providers()`, so a city due only on `panoramax` (rank 6) sorts behind **every** city due only on `kartaview` (rank 4), and filled from its head the kind hands every slot to KartaView.
The KartaView queue also refills faster than it drains: a KartaView city whose `gsv` succeeds tonight is opt-in-only tomorrow.
Measured on the prod slate for 2026-09-21: Des Moines was position 51 of 51 in that kind (kartaview 50, panoramax 1), and 19 of the 20 newly enrolled Panoramax cities had never collected, with `consecutive_failures` at 0 and no alert.
So the opt-in-only kind is itself a rotation over sub-queues keyed by each city's **leading** due channel — the earliest in `providers` order it is due on — each keeping union order, so stalest-first still holds within a channel.
A walk-only straggler (due only on `kartaview_streets`) therefore keys on the walk and gets its own sub-queue, deliberately: it is the population that would otherwise wait behind its own grid channel's.
With a single opt-in channel stranded the take is exactly the straight union-order take it replaced, and the other two kinds are unchanged.
**The channel rotation starts at a night-varying sub-queue** — the run date's ordinal modulo the number of non-empty sub-queues — because a start fixed at rank order's head fixes nothing on prod.
At a reservation of 10 with all three kinds non-empty the opt-in-only kind gets about 3 slots, so with four stranded channels (`kartaview`, `kartaview_streets`, `panoramax`, `panoramax_streets`) a fixed start handed `panoramax_streets` 0 every night.
The guarantee is exactly this and no more: while the set of non-empty sub-queues is unchanged and the opt-in-only kind receives at least 1 slot a night beyond its resumers — which the per-sub-queue resumer floor below delivers at a reservation of at least the number of sub-queues — every stranded channel is reached within (number of sub-queues) consecutive nights.

**Which cities take the reserved slots is a real question only once the hoist is bounded, and the answer is a live checkpoint first.**
`get_due_cities` orders `last_success_at ASC NULLS FIRST, city_id ASC`, and a city SIGKILLed mid-sweep still has NULL there — it never succeeded — so filling the reservation in union order sorts it **alphabetically** among every never-run enrolled city, which during a widening is the whole enrolled set.
Enrol 200 at a reservation of 5 and a killed city sorting late is not reached for ~40 nights, far past `CHECKPOINT_MAX_AGE_S` (7 days): its checkpoint is discarded, `_SWEEP_SKIP_AGE_WALL` records a real `consecutive_failure`, and the partial sweep is re-paid every cycle.
That would silently retire the "five nights must be CONSECUTIVE" property this section's own hoist rationale rests on, during exactly the widening the bound exists to enable.
So a city with a live checkpoint takes a reserved slot ahead of one never swept — the population the invariant is about, and the only signal that separates "already paid for, and the payment expires" from "never touched" (`consecutive_failures` would catch only the SIGKILL arm; a healthy multi-night pause records none).
The probe runs only when the reservation actually has to choose, so at today's enrolled set it costs no filesystem reads at all.
The starvation `WARNING` is **kept as a backstop, not deleted**: `cmd_run_due` still logs when the hoisted count reaches `max_cities_per_day`, naming the channels that will therefore collect nothing.
Two ways to reach it, neither of them the wide enrolled set the pre-#282 version fired on: an operator set `opt_in_cities_per_day` equal to `max_cities_per_day` and re-created the unbounded hoist by hand — legal and bad — or `--limit 1` made even a derived `max(1, 1 // 4)` equal the cap, which is a degenerate one-city run rather than a misconfiguration.
It is suppressed entirely when every requested channel is opt-in (`run-due --provider kartaview`), since there is then nothing to starve and the message used to name no channel at all.
`hoisted` counts cities that actually **moved**, not cities that were promoted: with a bound, an all-opt-in slate splits into promoted and unpromoted while remaining the identity permutation, so counting promotions would report a reorder on every catch-up.
Setting it to 0 switches the promotion off entirely without un-enrolling anybody.
That is the cost `docs/provider-access.md` records for `--provider` filtering, made permanent and universal; the expensive-city problem it would have solved is #239's, which does not require the trade.
**What lanes buy is lanes, not throughput.**
Each channel keeps its own limiter and its own daily budget, so no provider is asked for anything faster or larger than before; what stops is independent work queueing behind unrelated work.
**The safety argument is host affinity.**
The launch pass computes the set of per-IP hosts the in-flight siblings hold (from `CHANNEL_HOSTS`, never a hardcoded list) and *defers* — leaves pending, silently, reconsidered when a sibling completes — any channel that intersects it.
So each of Overpass, the Mapillary tile CDN and KartaView sees at most one talker from this process, exactly as before, and the configured `max_requests_per_minute` stays the real figure rather than doubling.
Every walk shares Overpass with every other walk, and shares its provider's host with its own grid channel, so it always runs after both — which is also the desirable order, since the second street channel of a city then hits the warm GraphML cache instead of racing for the same Overpass fetch.
With today's **eight** channels the largest host-disjoint set is therefore `gsv` (no per-IP host) + `gsv_streets` (Overpass) + `mapillary` (tile CDN) + `kartaview` (kartaview.org) + `panoramax` (api.panoramax.xyz) = **5 of 8** (#335).
`gsv_streets` is the Overpass representative because it is the only Overpass channel with no SECOND host: picking any walk instead gives up that walk's other host and the set drops to 4.
Re-deriving has now cost three different answers — 4 of 5, then 4 of 6, then 5 of 7 and 5 of 8 — and the instructive one is the last pair: the NUMERATOR did not move, because every walk added brings one more Overpass user and Overpass admits exactly one talker, so a walk can only ever displace the Overpass channel already in the set.
The figure is a property of the channel SET's host graph, never a constant — derive it again for a ninth rather than quoting this number.
The child-side per-host lock (#208) is unchanged and still covers the manual runs the parent cannot see.
**Everything except the child itself runs on the main thread.**
A lane worker calls `_run_one_city` and nothing else; pricing, both budget gates, the ledger read, the resource guard, the breaker and *all* classification (busy/blocked/salvage/killed-by-stop/`record_attempt`) stay on the thread that owns the catalog, because `db.connect` opens it `check_same_thread=True`.
The two values `_run_one_city` would otherwise derive from `conn` or the clock are precomputed at the launch site and passed in (`timeout_s`, `estimated_requests`); the scheduler hands the worker `conn=None` deliberately.
That also keeps the read-then-write budget guard honest — the reads are serialized by being on one thread, in submit order, so two channels cannot both see "under budget" and both spend.
**At the default of 1 the channel body runs INLINE on the calling thread**, not on a size-1 pool: that is what makes the default byte-equivalent to the pre-#240 loop and what keeps every existing test's `_run_one_city` substitute able to touch the fixture connection.
**Neither budget gate applies to a channel `CHANNEL_RESUMABLE` marks (#274, #318, #335), which is the two KartaView channels, the two Mapillary ones and the two Panoramax ones.**
Both gates exist because every other channel is all-or-nothing — a partial grid or road walk is not a run — so refusing to start is honest and `est > budget` is a real dead end.
A crawl that checkpoints is launched with `min(budget − used, what its timeout can pace)` as its cap whatever the estimate says, and its estimate is deliberately not consulted: it prices the WHOLE crawl even for a resuming city, because a paused one never reaches `register_run`.
That over-pricing is not a KartaView quirk — `docs/provider-access.md` records the same of the tile census, "the pre-flight estimate still prices the whole tile count even when a resume will fetch a fraction of it" — so New York would be re-priced at 484 and skipped a second time.
**What this actually changed on the ground:** the live win is the `used + est > budget` deferral becoming a capped launch — the end-of-night sliver that collected nothing from New York at 88% affordable, twice in six nights.
The permanent `est > budget` arm has **never** fired: measured on production 2026-09-10, no enabled city's geometry clears a whole Mapillary night (1,221 cities, median 15 tiles, largest Moscow at 870 against a 3,500 budget).
That took a query rather than a glance, because **a "largest grid" figure from a population of collected runs cannot answer it** — the gate skips exactly the expensive cities — and because a dev catalog in a checkout carries pre-#166 geometry that says otherwise; `docs/provider-access.md` has the numbers and the trap.
Worth knowing before one grows into it: neither budget arm applies to a resumable channel, so a city needing more than a whole night now takes the **entire** channel budget for `ceil(tiles / budget)` consecutive nights, with every other city those nights falling under the launch floor, and `NULLS FIRST` would put a never-collected city of that size first in the queue.
That is the right trade against collecting it never, but it is a whole-night decision and nothing in the log distinguishes it from an ordinary night.
A night therefore spends its budget rather than stopping short of it; across nights a resumed city is cheaper, because its tiles are paid once instead of re-paid.
Not *exactly* its budget, though — a capped crawl lets tiles already in flight finish their retries, so it can end up to `connection_limit × (TILE_MAX_TRIES − 1)` over — **20 on every tile census**, grid or walk.
`cli.py` forwards `--connection-limit` only on its `gsv` arm, so a Mapillary or Panoramax grid runs at the downloader's own default of 5; a walk receives the scheduler's per-child share (`[download].connection_limit` divided by the lane count and clamped at `MAX_PER_CHILD_CONNECTION_LIMIT`, 50 in prod), narrowed to its provider's own ceiling, which is 5 for both Mapillary (since #361) and Panoramax.
Until #361 the Mapillary road walk's ceiling was 50, so its residue was 200.
The single floor left is `_crawl_pricing(channel).launch_floor`, and it is read against that final cap rather than against the budget remainder.
It is **per provider, because the failure it prevents is a different failure**: a radius sweep that runs out during *calibration* raises a plain `DownloadError` rather than `SweepIncompleteError`, so it takes no amnesty and counts a real failure; a tile census has no ladder, but a cap under one tile's full retry budget can spend itself whole on a transiently-404ing tile, commit nothing, and still cost a night and a day of the checkpoint's seven.
Both are derived from the collector's own constants — the ladder's documented bound, and `TILE_MAX_TRIES` — so retuning either carries its floor along.
The floor is applied only when `est > 0`: a walk whose census is already in the shared cache never crawls at all, so the crawl's cost cannot be a reason to defer a channel that will spend nothing (#290).

**A resumable channel has three skips, and only one of them records a failure.**
The launch decision lives in `_sweep_launch_plan` and is read by both the live launch site and `run-due --dry-run`, so the preview an operator checks before a night cannot disagree with what the night does — it used to print `OVER BUDGET (deferred)` for exactly the metros the live path launches capped.
The skips, in the order they are asked: a **walk deferred** behind its grid sibling's in-flight sweep of the same lattice (an INFO line and its own counter, no failure, and it collects for 0 once that sweep lands in the cache); a checkpoint **at the age wall**; and the calibration **floor** above.
The age wall is the one that records a failure, deliberately.
`CHECKPOINT_MAX_AGE_S` is measured from the checkpoint's first commit and the *child* discards an older one, then re-commits with a fresh stamp — so an under-budgeted city re-sweeps from root 0 every seven days, forever, while every night reads as ordinary progress and a pause records nothing an alert can see.
Within a night of the wall, with more lattice left than the night's cap can cover, the scheduler logs at WARNING, refuses to launch and records a real failure for that (city, channel), which is what puts it in `attempted − succeeded` for the nightly alert and eventually quarantines the city through the five-night backstop instead of discarding ~60k requests a cycle.
"Cannot finish tonight" is projected from what the checkpoint has answered against the estimate, so a crawl on its last night with one unit left is still resumed.
`checkpointing.sweep_progress` reads **both store shapes** to answer that — a radius sweep records `root_count`/`roots_done`, a tile census `tile_count`/`done_tiles` — and reading only the sweep's was a fail-QUIET rather than a crash: its best-effort `except` swallowed the `KeyError` and returned `None`, which every caller reads as "there is no checkpoint", disabling the one arm that can see a checkpoint being discarded weekly.
Adding a resumable provider means adding its layout there, not only its flag to the table.

The property is declared as data, not as `provider == "kartaview"`: `CHANNEL_RESUMABLE` means "accepts a request cap that pauses and checkpoints rather than failing".
That is a stronger claim than "checkpoints", and it is why both Mapillary channels were `False` for the whole of #256 — the census resumed after an interruption it did not *choose*, but `download_mapillary` took only a pacing knob and had no number to stop itself at.
#318 gave it one, so both flip.
**`panoramax` is the instructive `False`**: its downloader has the identical cap since #318, and what keeps it out is that the grid argv built inline in `_run_one_city` has no arm to forward one — a `True` nothing downstream reads is exactly the fail-open the table exists to prevent.
Flip it in the same commit that adds the launch arm.

Each walk is `True` beside its grid run because it reads the same census by the same crawl, so its `--daily-budget` is a gate priced from an estimate rather than a ceiling on what the crawl spends; `_street_collect_cmd` passes **both** flags, and they are not redundant — the budget is the full ceiling the collector subtracts today's spend from itself, the cap arrives already subtracted.
Which is also why each walk's cap is asserted at the command, not merely at the flag's default.

**The clock term must read the constants that TIME the channel, not another provider's.**
`_sweep_requests_within_timeout` is the inverse of a timeout derivation, and there are two: a radius sweep is timed at `DEFAULT_SWEEP_REQUESTS_PER_MINUTE × 0.5`, a tile census at `DEFAULT_TILE_REQUESTS_PER_MINUTE × 0.8`.
Inverting one channel's clock with the other's constants under-prices a Mapillary cap — and a cap below the launch floor does not slow a channel down, it **skips the city outright, nightly and silently**.
By how much depends on whether the channel is configured, and the two answers are far apart: with prod's `[providers.mapillary] max_requests_per_minute = 40` the configured rate is used on both sides, so only the fraction was wrong (40 × 0.5 = 20 against 40 × 0.8 = 32, **1.6×**); with no block at all the default is wrong too (16 × 0.5 = 8 against 60 × 0.8 = 48, **6×**).
An earlier telling of this said "roughly fourfold at the shipped rates", which is the one case where it is 1.6× — the figure came from pairing KartaView's *default* 16/min with Mapillary's *configured* 40/min, two branches the function never takes together.
The four numbers that size a resumable launch (default rate, achieved fraction, launch floor, and what that floor buys) therefore live in ONE row per provider (`_CRAWL_PRICING`), so the two directions cannot disagree; an unpriced resumable channel is a `KeyError`, the same posture `CHANNEL_RESUMABLE` takes.

**A resumable channel reached at the very end of a night now defers instead of launching.**
The clock term holds `_TIMEOUT_FIXED_SLACK_S` back for process startup and the checkpoint write, so once the deadline clamp puts a city's timeout under that, the requests the clock affords is 0, the cap is 0, and the launch-floor arm defers.
Before #318 the same city launched under a short clamped timeout and was usually SIGKILLed by it — which loses the ledger write and counts a `consecutive_failure`, where the deferral costs neither and the city stays due.
It is the better trade and it is also a live behaviour change on the busiest channel: expect the tail of a long night to show `deferred (0 req under the launch floor)` where it used to show a killed child.

**Deferral and a final skip are different things and must stay different.**
A budget skip, a breaker skip and a stop are decisions: the channel leaves the pending list and is never reconsidered tonight.
A host deferral is not a decision at all — nothing was priced, nothing was logged, and the channel launches the moment its sibling frees the host.
Conflating them either re-prices skipped channels in a loop or drops deferred ones on the floor.
The no-livelock invariant that makes the loop terminate: an empty in-flight set at the top of a launch pass means an empty host set, so nothing can defer — every such pass launches, skips everything, or is stopped.
**Classification drains before the next launch pass, and that is a correctness invariant rather than a convenience.**
`streetwalks.json.gz` has three writers through `json_summarizer._write_json_gz_atomic`'s fixed `path + ".tmp"`: the street child's own end-of-walk rebuild, the parent-side `_reconcile_orphaned_walk` salvage, and the batch tail.
Host affinity keeps at most one street child alive; draining classification (salvage included) before launching again keeps a salvage rebuild from overlapping the next street child's tail write.
The GraphML torn-cache hazard is covered by the same gate on child **exit** — `ox.save_graphml` runs after the Overpass lock releases but before the process exits, and a channel's hosts are held until its future completes, so two Overpass processes never overlap at all.
**Semantics that shift above 1, documented rather than fixed:** one city's per-channel log lines interleave, and classification lands in completion order (the per-attempt child logs are untouched — unique per (city, channel, date), append mode).
The summary's `elapsed_h` becomes concurrent wall clock; its role as a proxy for Mapillary time-under-load survives, because the two Mapillary channels still never overlap.
Counters mean what they meant.
Ledger races are impossible (five channels are five `api_usage` keys, and the city-level drain keeps cross-city reads ordered), and a busy-skip caused by *our own* lanes is structurally impossible rather than merely unlikely — which is why any 79/80 on a night with no manual run means a hole in the affinity gating and should drop the knob to 1 the same day.
Dropping the knob is safe to do alone **because of `MAX_PER_CHILD_CONNECTION_LIMIT`, not because the numbers happen to work out**: `connection_limit` is divided by the lane count, so at prod's 100 a drop to one lane would otherwise hand every child 100 sockets — a socket *raise*, mid-incident, from the one action the incident playbook asks for.
The clamp puts it back at 50 and logs that it did; nothing else needs editing in the same breath.
**`[download].connection_limit` is divided across lanes, not handed to each child whole.**
The resource guard reads host-wide pressure and only ever *lowers* its answer, but it is consulted once per child from a sample taken before that child's siblings have ramped — so at N lanes each child reads a quiet box and each takes the full limit, and the guard structurally cannot see the load it is about to permit.
Only four channels carry the number at all: the `gsv` grid, the `gsv` road walk, the Mapillary road walk and — since #335 — the Panoramax road walk.
The two census walks then clamp it to their provider's own ceiling of 5 (Panoramax since #358, which exists because that walk was taking the scheduler's 50 against a volunteer-run host whose own grid runs at 5; Mapillary since #361, for the same asymmetry on the tile CDN).
The Mapillary and Panoramax **grids** never receive it (`cli.py` omits the argument, so each downloader's own default of 5 applies).
So the only children the share actually sizes are the two GSV ones: at knob 3 each gets `100 // 3 = 33`, and every census child stays at 5.
Combined with affinity, the only overlapping pair that points two full-size connectors at one third party is `gsv` + `gsv_streets` — both Google — which is 100 concurrent sockets on the same endpoints gate (2) below is already about.
Dividing makes the knob a no-op at 1 and bounded above it; the trade is that a city with a single enabled channel gets the divided share too, so **raise `connection_limit` deliberately when you raise the knob** rather than discovering the multiplication in production.
Since 2026-09-21 the divided share is additionally clamped at `MAX_PER_CHILD_CONNECTION_LIMIT` (50, the figure the systemd unit was sized against), which is what makes *lowering* the knob safe on its own — without it, division turns a knob drop into a per-child socket raise.
A **street** child is then narrowed once more, by its provider's own walk ceiling (`download_common.WALK_CONNECTION_LIMITS`): `_street_collect_cmd` sends `min(share, ceiling)`, so `mapillary_streets` (since #361) and `panoramax_streets` each hold at most 5 sockets however this knob and `[download].connection_limit` are set, while the division, the guard and the clamp above still lower it below that.
Until 2026-09-21 the share was sent unconditionally, which made each provider's own default unreachable on every scheduled walk — see [`docs/street-coverage.md`](street-coverage.md).

**Two things gated raising it in production. The first is now satisfied; the second is still outside this repo.**
(1) **Resume for every provider**, because a deadline or a `systemctl stop` now kills up to N children at once instead of 1.
This is **met as of #256**: GSV grid (`.downloading` sibling), the GSV road walk (same `collect_points_async` engine), KartaView (`checkpoints/`, #239) and both Mapillary channels (`checkpoints/`, #256) all resume,
so a killed child costs the tiles it had not yet fetched rather than the ones it had — which mattered here because a re-spend lands against the deliberate per-IP ceiling — since #385 the 3,000-per-rolling-24-h host pool, which binds before 5,250, the mere sum of the two daily budgets (#286) — i.e. ban risk rather than merely lost time.
A killed child still records no `api_usage` at all (#238), and that loss multiplies by N — unchanged by the checkpoint, since it is the parent that never sees the number.
(2) **`gsv` and `gsv_streets` hold no per-IP lock**, because Google meters per Cloud *project* rather than per IP — so running them together is only safe while `GMAPS_API_KEY` and `GMAPS_STREETS_API_KEY` really do live in **separate projects**.
The projects **are** now recorded — in `config/scheduler.makelab1.toml` beside `max_concurrent_channels`, with the account and the date of the check, as of 2026-09-21; this file said they were recorded nowhere until then.
Treat that record as re-confirmable rather than settled: the two were told apart by *traffic signature* rather than by reading key strings, so a key rotation needs a fresh console check.
A shared project must be fixed by splitting the keys, never by inventing a fake `CHANNEL_HOSTS` entry (that would couple the night-level breaker to a condition that is not a host refusal).
**Rollout:** land at 1 everywhere and diff two nights' summary lines against history to check the byte-equivalence claim in production; then the two gates above; then flip prod to **2 before 3**, since `gsv` is the long pole and already overlaps each short channel in turn at 2, for half the blast radius of the unmeasured (cgroup memory sum, log interleaving).
**The 5-of-8 ceiling and what tonight's catalog can actually use are different numbers, and the second one is measured rather than derived.**
5-of-8 is a property of the channel set's host graph.
Against the prod catalog on 2026-09-21 (1,221 enabled cities; `kartaview` and `kartaview_streets` 502 enrolled each, `panoramax` and `panoramax_streets` 20 each), the largest host-disjoint set any city can present is **4** — `gsv` (no per-IP host) + `gsv_streets` (Overpass) + `mapillary` (tile CDN) + `kartaview` — because every remaining channel needs a host one of those four already holds.
**A fifth lane is unreachable on any city:** it would need `panoramax` alongside `kartaview`, and the overlap of the two enrolled sets is **exactly zero cities**.
So 3 and 4 are usable today and 5 is not, which is the opposite of what an earlier draft of this section said; widening Panoramax past its 20 would change that without touching the 5-of-8 figure, so re-measure rather than quoting either number from memory.
Watch `MemoryPeak` after each night rather than pre-raising the unit's `MemoryHigh=40G`/`MemoryMax=48G` (raised from 20G/24G on 2026-09-02 under #305; crossing `MemoryHigh` throttles *all* lanes into the documented reclaim stall), and if they must move, move High and Max together, keep the unit's quoted prose figures in step, and re-copy + `daemon-reload`.
**Read the unit, not a paraphrase, on what it was sized for**: it says "exactly three *things*", whose second item is **two** children at once with an explicit warning that 2 × 18.88 GiB is 94% of `MemoryHigh` — a list-item count, not a child count, and misreading it as three is what licensed the raise this section gates.

**Production flipped 1 → 2 on 2026-09-21, and `[download].connection_limit` 50 → 100 in the same change** — the two move together or not at all, for the division reason above: left at 50, two lanes would have handed the `gsv` grid child 25 sockets and made the long pole this change exists to hide work underneath roughly half as fast.
`100 // 2 = 50` keeps every child on exactly today's socket count, so the knob buys overlap without paying for it out of GSV's throughput.
**Going to 3 lanes at today's per-child 50 does need 150 here** — a draft of this section retracted that advice as unsafe, and the retraction was wrong: it described a hazard the clamp had just closed, in the present tense.
Measured per-child share: at `connection_limit` 50 the lanes give 50 / 25 / 16 / 12; at 100, 50 / 50 / 33 / 25; at 150, 50 / 50 / 50 / 37.
**The division, not the clamp, sets the share above one lane**, so raising `MAX_PER_CHILD_CONNECTION_LIMIT` is the wrong lever for reaching 3 — at 100 over 3 lanes every child gets 33 and the clamp never bites, so an operator who raises only the constant gets 33 sockets and no explanation.
The old hazard is genuinely gone: the clamp caps every child at 50 and the `cli.py` guard is `connection_limit > PIPELINE_DEPTH × batch_size` (4 × 100 = 400 since #304 pipelined the GSV engine; `batch_size` alone before), so **the scheduler cannot reach that guard at any value of the key**.
It stays reachable by hand — see #359, which also argues an argparse exit 2 should not count a strike against a city.
Raising a child *above* 50 is the separate decision that needs a memory measurement first, and a `batch_size` raise only past 400.
The memory gate is **argued, not satisfied**: the `cgroup peak` line recorded every night from 2026-09-07 to 09-20 reads **1.30–7.89 GiB, 3–20% of `MemoryHigh`, throttled 0 times** — fourteen clean nights, all of them at *one* lane, and the unit additionally asks for a week that includes a 4M-point city, which that range is itself evidence was absent.
The figure still worth watching is not that range but the **worst city** — a single child read 18.88 GiB on 2026-09-01 — because a big city is big on every provider at once, so two such children in one city is ~38 GiB: under `MemoryMax`, over `MemoryHigh`, i.e. the throttling-hang failure #305 documents rather than a legible OOM.
`TimeoutStopSec=30min` needs no change: it prices the tail, which concurrency does not touch, and N children wind down in parallel.
The before/after is a measured question and therefore owes a writeup: `scripts/night_length_analyze.py` lands with the code and reads the elapsed distribution (with per-channel `api_usage` and the busy/blocked counts beside it, as the volume control) straight out of `logs/streetscape_scheduler.log*`; `docs/experiments/night-length.md` follows once there are nights on both sides of the flip to compare.
That is also why `cmd_run_due` logs `max_concurrent_channels=N` **and `connection_limit=N`** on its opening line — which setting a night ran under has to be recoverable from the night's own record, not from an operator's memory of the flip date.
Both, because the per-child socket count is the pair divided: a night that fell back to one lane would otherwise be grouped with pre-flip one-lane nights while having run at a different share.

## A shared rolling-24h budget per per-IP host (issue #385, added 2026-09-29)

**Every channel on a budgeted per-IP host draws from one pool, counted over the last 24 h rather than per UTC date.**
It is configured per host token, `[hosts.mapillary_tiles] rolling_24h_request_budget = 3000` on prod, so both Mapillary channels are covered, and so would be a future channel on the same CDN.
The per-channel `daily_request_budget`s stay exactly as they are.
**The effective remainder at a launch is the minimum** of the channel's daily remainder and every budgeted host's rolling remainder.
The rule lives once, in `_combine_remainder`: `_budget_remainder` feeds it the ledgers as they stand for the live gate and `assess-city`, and `run-due --dry-run` feeds it one window read plus its own simulated spend per host, so the preview draws the pool down across both channels the way the night will.
**A host governs only when its remainder is strictly smaller**; on a tie the channel's daily budget is the named term.
A resumable channel is then capped at that remainder, or deferred under its launch floor, by the machinery above; no new stop path exists.
**A floor skip is the host's only when the host's window set the cap** (`BudgetRemainder.host_bound`: the host governs AND the cap equals the ledger remainder) — a skip the deadline clamp caused is the clock's, and stays a budget skip even while the host is the smaller ledger term.
**The age wall asks what tonight could plausibly grant, not what the window holds right now** (`BudgetRemainder.ceiling`: the channel's daily remainder against, per budgeted host, its budget minus the spend that will still be in the window when the batch ends — everything stamped at or after `batch_end − 24 h`).
It is the one arm that records a failure, and at 09:00 UTC after a full night the window holds ~53 of 3,000 while most of the rest frees before the night ends; projected against that, a 6-day checkpoint with a few hundred requests left would be failed and the operator told to raise the wrong budget.
The full budget would be wrong the other way: late in a night that has itself spent ~2,947, none of it ages out before the batch ends, so a crawl the wall passed against 3,000 would launch capped, never finish, and be discarded past `CHECKPOINT_MAX_AGE_S` with no failure recorded — the silent weekly re-sweep the wall exists to alert on.
`batch_end` is the batch deadline's wall-clock equivalent on the live path and `now + max_batch_hours` in the dry run; `assess-city` and any other caller without a deadline use `now`, so the ceiling is the momentary remainder — the conservative reading on an operator run.
When the host is what cannot fit the crawl, the refusal names `[hosts.<token>].rolling_24h_request_budget` as the lever; when a timeout is the smaller term it names no budget, but the clock that binds: the batch deadline's clamp on a late launch (`[schedule].max_batch_hours`, or run the city earlier), or the city's own derived timeout (`[schedule].city_timeout_minutes` is its floor, and it grows with the grid).
With no `[hosts]` section (the repo default), or an empty one, the gate reads exactly what it read before.
An invalid entry (a token no channel's ledger meters — `[hosts.overpass]` included, which names a real host but could never bind — a non-positive or non-integer value, a stray key) is recorded and logged at load like an unwired channel, and `run-due` and `assess-city` refuse with 64: falling back to "no budget" would be the fail-open direction.

**It is re-read per launch, never once per night**, because the same night's earlier children on the host have written to the ledger since.
The two Mapillary channels never overlap (the cross-process host lock, and host-disjoint lanes in-process), so no cross-lane reservation is needed.

**The ledger is `host_usage` (schema v16), written by `db.add_api_usage` itself** for every channel in `download_common.CHANNEL_METERED_HOST`, so no call site can forget it and the ledger is complete whatever the budget config says.
A child records its spend when it finishes, so a long crawl's whole spend is stamped at its end.
That shifts spend **later** within the window, which makes the gate slightly more conservative on the following night, never less; there are deliberately no mid-crawl writes.
**The design consequence is lumpy credit.** The first Mapillary launch after a full night sees the small remainder and is launched capped at it, and credit returns in lumps as last night's per-crawl stamps age out, so a big crawl fragments into capped slices across the night; the dry run reads the window once and ages nothing, so at preview time it over-reports deferrals the night itself will launch.
**A child SIGKILLed or crashed mid-crawl writes neither ledger**, since both writes happen when it returns; PR #387 gives resumable children a clock stop, which shrinks that case without closing it.
The v16 migration backfills the last two UTC dates of metered `api_usage`, each row stamped at the latest instant its spend can have happened — `min(23:59:59 UTC of its date, the migration's clock)` — so the first night after deploy is gated for the **whole** night.
Noon was the first choice and was wrong: prod's night runs ~09:00–21:00 UTC, so a noon stamp released yesterday's spend at 12:00 UTC, three hours into the first night, while most of it was still inside the true 24 h.
The late stamp errs the fail-closed way: it can defer up to one night more than a timestamped ledger would have, and it never releases spend earlier than the real requests would have left the window.
The tail prunes rows older than 30 days, best-effort.
`import-bundle` writes no host row, since imported spend came from another machine's IP.

**It is a soft ceiling, exactly like the daily budget**: tiles already in flight finish their retries, so a capped night can end up to `connection_limit × (TILE_MAX_TRIES − 1)` over it.
Never write that it is not exceeded.

A host-governed deferral is logged with the host, its usage and the window start, counted in `deferred_host_budget` rather than `skipped_budget`, and reported on the `Done:` line as `N deferred for the rolling-24h budget of <host>`; a host-capped launch names the host in its cap line.
`run-due --dry-run`, `scheduler status` and `assess-city`'s pre-flight all print the window.
A direct `streetscape_tracker.py --provider mapillary` has no scheduler config, so it neither warns nor refuses; its spend still lands in the window through the seam.
**This is a staging guard on how fast our traffic can change, not a model of Mapillary's per-IP threshold** — see `docs/provider-access.md`, block 4.

**Deploying v16, and rolling it back.**
`import-bundle` requires the bundle's catalog to be exactly this host's `SCHEMA_VERSION`, so once v16 is deployed it refuses every v15 laptop bundle: import a waiting bundle **before** deploying, or re-collect it on a v16 checkout.
Pre-#385 code refuses a v16 catalog ("newer than this code supports") on every subcommand that opens it through `db.connect` — `run-due`, `status`, `assess-city`, `import-bundle` and the rest — so a rollback needs `PRAGMA user_version = 15` set by hand on the catalog first; the extra `host_usage` table is harmless to old code.
`backup-status` and `restore-backup` read only the backup directory, so they keep working either way.
Rolling back past #367 (v17) as well needs more than the stamp: old code builds `RunRow(**dict(row))` from `SELECT * FROM runs`, so the two query-radius columns must be dropped (`ALTER TABLE runs DROP COLUMN status_out_of_radius`, then `query_radius_m`) before it can read a run.
Re-deploying after such a rollback does **not** re-backfill (the table is no longer empty), so spend made while rolled back is missing from the window until it would have aged out anyway.

## What a capped night spends its slots on (issue #308, added 2026-09-02)

**Breadth-first was never a decision; it was the shape of a tiebreak, and this section is that tiebreak given a name and a lever.**
`db.get_due_cities` orders `last_success_at ASC NULLS FIRST, city_id ASC`, so every city that has never succeeded on a channel sits ahead of *every* refresh, and the all-NULL block drains alphabetically.
Measured on prod 2026-09-01: 497 of 1,216 enabled cities had never had a successful scheduled `gsv` run and 506 had only their legacy migrated baseline, so at the then-cap of 20 the never-collected block had ~25 nights left to run — and **until it drains no city gains a second dated interval**, which means `run_diffs`, #101's walk diffs and the run-to-run change summaries this project exists to produce have nothing to compare and never exercise.
The run history shows it plainly: the batch marched the alphabet, `la*`/`lo*` the week of 2026-08-25 and `ma*`–`mo*` the week of 2026-08-30.

**The hoist's key is STRANDED — not due on the union's rank-0 channel — rather than "due only on an opt-in channel" (#301).**
The union is ordered by first appearance across `enabled_providers()`, so a city's slate position is set by the earliest channel it is due on, and one not due on `providers[0]` is appended behind every rank-0-due city and then truncated by the city cap.
Which channel put it there does not change that, and keying on the cause left half the population unrescued: a city excluded from `gsv`/`gsv_streets` fails `all(p in opt_in ...)`, because `mapillary` is a default-membership channel, and `_reserve_refresh_slots` cannot reach it either since that needs a non-NULL `last_success_at` and such a city has never collected.
Measured before the key changed: ten gsv-excluded cities behind 45 gsv-due ones landed at union positions 45–54 and collected **0 of 10** at prod's 40-city cap — indefinitely, because unlike an ordinary stalled city they never re-enter gsv's due list at all, and with `consecutive_failures` at 0 the night's own starvation diagnostic could not see them.
The key is the **union** of the two conditions, not a replacement: a filtered widening (`run-due --provider kartaview`) makes rank 0 the opt-in channel, so nothing is stranded by the first half while the live-checkpoint preference below still has to hold.
`[schedule].opt_in_cities_per_day` keeps its name — renaming a deployed config key to track a widened meaning is not worth a production edit — but it now bounds the rate at which ANY stranded population is worked off, so a KartaView widening and a #301 rollout share it.
**They share it by round-robin over THREE populations, not by arrival order**, and both halves of that are load-bearing rather than tidy.
Taken straight down the union the largest group takes every slot, because union order inside one channel's due list is `last_success_at ASC NULLS FIRST, city_id ASC` and a stranded block is all-NULL there, so the winner is decided alphabetically — a lottery on `city_id`, not a rate.
The three groups are what the reservation has to tell apart, and the question each answers is **whether waiting fixes it**:

1. **excluded from rank 0** (`schedule_state.member = 0`, #301) — permanent; nothing but this reservation ever reaches it;
2. **due only on opt-in channels** (#248) — effectively permanent while the sibling default channel keeps succeeding;
3. **transiently not due on rank 0** — its rank-0 clock is merely fresh, and it rejoins the union head by itself within a cycle.

Grouping only on 1-or-2 versus 3 is not enough, and the first attempt at this shipped exactly that: group 3 is prod's documented ~121-city mapillary-only-due population, and sharing a bucket with group 1 it won the same alphabetical lottery, putting the #301 cities back at **0 of 10** on a prod-shaped slate.
Splitting 1 from 3 is what makes the reservation a rate: measured on the same slate, 4 of 10 to #301, 3 to the KartaView widening, 3 to the transient population.
**A live checkpoint outranks the rotation across every group**, taken first and in union order — confined to its own group it stops being a guarantee at a reservation of 1, and #239's five nights stop being CONSECUTIVE.
That take is itself **bounded**, leaving one slot per other non-empty **sub-queue** — each of groups 1 and 3 is one, and group 2 counts one per stranded opt-in channel, the rotation's own sub-queues (#348) — because unbounded it is the same starvation a third time: measured, ten stranded resumers in one group displaced the other two groups one-for-one and zeroed both.
Counting group 2 as ONE was the same starvation a fourth time, one level in: resumers reach group 2 without passing through its channel rotation, so on #348's prod-shaped slate (reservation 10, groups 1 and 3 non-empty, four stranded opt-in channels) eight live KartaView checkpoints took eight slots and `kartaview_streets`, `panoramax` and `panoramax_streets` got **0 on every night** those checkpoints stayed live, up to `CHECKPOINT_MAX_AGE_S` (7 days).
Counted per sub-queue the take there is at most 5, not 8, so checkpoints drain more slowly and wait longer against that seven-day age — the price of the channel rotation still getting a take every night.
The floor keeps it at one, so at a reservation of 1 the resumer still wins and the #239 guarantee is untouched — and it never exceeds the reservation, so a reservation of **0** hoists nothing, resumers included (the bare `max(1, …)` used to take one even then).
**A chosen resumer IS its sub-queue's turn (#393)** (groups 1–3 here are `_stranded_kind`'s kinds 0–2, the numbering the code and `docs/testing.md` use): a group goes to the back of every pass only once every sub-queue it still holds was served by a chosen resumer, and the rest go first, each half in group order.
For groups 1 and 3, one sub-queue each, that is simply "a resumer came from it"; group 2 stays at the front while any of its stranded channels has had no resumer.
Restarting at group 1 regardless handed the leftover slots to groups 1 and 2 when the resumer was in group 1, so group 3 got nothing at a reservation of exactly three — and moving the rotation's START to the first unserved group is not enough either, because with the resumer in group 2 that rotation runs 1, 2, 3 and zeroes group 3 the same way.
Only resumers the floor actually took count as served; one it cut is still in its group's queue, and that group still gets its turn.
The guarantee is **per night**: with S non-empty sub-queues, a reservation of at least S gives every non-empty group a slot, and every group still holding an unserved sub-queue a slot **beyond its resumers**, because the floor takes at most `reservation − S + 1` resumers and so leaves at least as many slots as there are front-half groups.
That second half is what the channel rotation's across-nights guarantee above needs.
Below S the served groups have had their turn and the rest fill in group order, so the highest-numbered unserved groups wait; rotating which group waits across nights would need state carried between nights, and is not done.
A group leaves the rotation as it empties, so a night with only one stranded population behaves exactly as the straight take did.

**`[schedule].refresh_slots` reserves a share of the night's city cap for cities that will gain a second interval.**
Unset it derives `max_cities_per_day // 4`; deriving let the split follow the cap while the cap was a night's size, but prod SETS it to 10 since the cap began a staged raise on 2026-09-25 (40 → 80 first), so it does not grow with every step; an explicit integer overrides, and **`0` restores the pure breadth-first order exactly** — the identity permutation, provable by construction rather than argued, the same property `max_concurrent_channels = 1` keeps for #240.
A bad value warns and falls back to *deriving*, never to 0, because 0 is meaningful here and a typo must not be indistinguishable from a deliberate policy choice; TOML booleans are Python ints, so `false` is excluded explicitly.
The promotion is order-preserving in both directions: promoted refreshes keep their stalest-first order and **lead** the slate (right after the bounded hoist), and the cities they displace are the window's **last** non-chosen ones, kept in relative order immediately after the window so they lead tomorrow rather than falling back into the ~900-city tail.
#308 shipped with the refreshes at the **end** of the window instead, so the night's head stayed breadth-first; that is only safe while the city cap ends the night, because then the whole window runs.
With `max_cities_per_day` set above what a night can reach (so `max_batch_hours` governs), the end of the window is where the deadline cuts, and the early return the reserve took whenever the whole due slate fit inside the window left every refresh behind the NULLS FIRST block — either way a clock-stopped night ran zero refreshes and nothing reported it.
At the head they run whichever limit ends the night, and `promoted` counts refreshes that actually moved, the same definition `hoisted` uses.
The window's set usually matches #308's but not always: #308 displaced the window's last cities whatever they were, so it could evict a refresh already inside the window; this version never holds fewer refreshes in the window.

**It cannot refresh a city early, and that property is not the reserve's to keep.**
Everything it can promote came out of `get_due_cities`, which returns nothing whose last success is under `cycle_days − grace_days` (83 days on prod), so a promoted "refresh" sits at the same staleness wall as every city it displaced — it is competing for a slot, not jumping a cadence.
A city is counted as a refresh when **any** channel it is due on tonight has a prior success, `any` rather than `all`: a city due on `gsv` (one prior run) and `mapillary` (never) yields one second `gsv` interval, which is exactly the outcome being accumulated, so requiring every channel to have a prior would exclude it for having a newly enabled sibling.

**The bounded opt-in hoist is applied first and the reserve second, into the slots the hoist did not take.**
The hoist still wins — its cities lead the slate, which is what keeps a paused sweep's five nights *consecutive* and so the only thing that makes #239's checkpointed progress accumulate — but it wins a **bounded prefix** now (#282), which is what makes this order available at all.
This is the reverse of the order #308 shipped with, and the reversal is a fix rather than a preference.
`_reserve_refresh_slots` then landed its promotions at the **end** of the window (it leads with them now, above) and the hoist displaces the window's **last** cities, so reserve-then-hoist evicted precisely what the reserve had just promoted: on the derived pair at prod's cap (10 opt-in + 10 refresh of 40) `refresh_slots` cancelled itself on exactly the nights a KartaView widening made the hoist do anything, while still logging `10 promoted`.
Hoist-then-reserve gives each reservation its own slots, and at a cap too small to hold both the reserve reports `0 promoted` rather than a promotion the night will not reach.

**So a night's cap is split three ways: at most `opt_in_cities_per_day` opt-in-only cities, then at most `refresh_slots` refreshes in what remains, then pure stalest-first.**
It is the **sum** of the two reservations that bounds how much of a night the plain queue still governs — 20 reserved slots lead a prod night, and, as the cap is raised in stages, the deadline increasingly ends it — so raising either one is a decision about the other, and `test_makelab1_production_config_is_wired` pins the sum for that reason.
A hoisted city that is itself a refresh is not counted against `refresh_slots`, so a night can exceed it: the key is a floor on second intervals, not a ration of them.
`cmd_run_due` logs `refresh_slots=N (M promoted)` on its opening line **unconditionally**, unlike the `hoisted=` clause, because the reserve is live by default and its derived value follows `max_cities_per_day`, so which policy a night ran under is not recoverable from the config file alone once either knob has moved.

**The alphabetical tiebreak is also a geographic bias, and that is worth recording even though the ordering did not change.**
`city_id` embeds country and state, so the all-NULL block is not drained in a random order: a given week's collection is correlated by name, and any interim analysis of "what we have collected so far" inherits that correlation.
Describe partial coverage accordingly.

**Two knobs moved with it, and neither was measured before (2026-09-02).**
`max_cities_per_day` 20 → 40: 20 bound only on *light* nights and threw away wall clock the deadline had already granted — 2026-08-30 stopped at 20 cities after **6.77 h** of a 10 h window, while 2026-08-27 hit the deadline at 14 cities and never reached the cap at all (#304).
`max_batch_hours` is the real governor — it stops starting cities, clamps the in-flight child (or, since #373, defers a non-resumable one that would not fit) and still runs the tail — so the cap is now high enough to let it be the only one, and more cities *sequentially* does not raise peak memory (one collection child at a time at `max_concurrent_channels = 1`), leaving #305's `MemoryHigh` headroom untouched.
That last clause is about the **cap**, not about the knob, and it stopped describing production on 2026-09-21: at `max_concurrent_channels = 2` a city runs up to two collection children at once, so the headroom is no longer untouched — it is spent deliberately, against the measurement recorded in the lanes section above.
`max_batch_hours` 10 → 12: 10 was a "comfortably below `TimeoutStartSec`" figure with nothing behind it, and the real bracket is `TimeoutStopSec` (30 min) < `max_batch_hours` < `TimeoutStartSec` (14 h) less the bounded tail (`PUBLISH_TIMEOUT_S` + `_MEASURED_TAIL_AGGREGATE_S` + `BACKUP_TIMEOUT_S` = 1,635 s ≈ 0.45 h).
12 clears both with 1.55 h spare and needs **no** change to the systemd unit; past ~13.5 h `TimeoutStartSec` has to move first, and two costs come with it — the publish lands later in the working day, and a longer nightly window is more sustained hours against the per-IP metered hosts, the axis Mapillary's blocks are currently suspected on (`docs/provider-access.md`).
Note the second-order effect the 12 h batch has on #273's cap arithmetic, recorded above: at 10 h the clock was the smaller of the two ceilings and the budget remainder was unreachable, and at 12 h that has swapped.

**The GSV daily budget followed a year later than it should have (2026-09-13, #304).**
`[providers.gsv].daily_request_budget` 10M → 15M.
The 20 → 40 city raise above was shipped without it, and the budget became the ceiling the cap used to be: 2026-09-11/12/13 spent **9,990,643 / 9,997,308 / 9,992,049** against the 10M cap — 99.9% every night — while deferring 20 / 14 / 16 cities for budget.
That counter is not channel-keyed — one int covers every provider — so read 20 / 14 / 16 as upper bounds on the gsv share rather than as a gsv-only count.

Read what that deferral actually costs carefully, because the summary line understates it.
A city whose gsv run does not fit the remainder keeps running its other channels, so `city_attempted` is true and it still counts against `max_cities_per_day`.
Those nights therefore processed a full 40 cities with up to half of them missing the grid run that is the point of the night — the loss is in the *composition* of the 40, not in a smaller number.
Raising the budget converts those into real runs inside the same 40 cities: no extra cities, and no extra requests to any per-IP metered host.
Mapillary and KartaView keep their own daily budgets, untouched by this line; Overpass is a **host** rather than a provider, so it has no `[providers.*]` budget to leave untouched at all, and this knob sends it nothing either way.

Sized against idle wall clock rather than against a quota.
Those three nights ran **9.18 / 7.61 / 5.87 h of the 12 h window** and all three ended on the city cap, leaving 2.8–6.1 h unused; at the measured median **30,137 req/min** (n=20) 2.8 h is ~5.1M requests.
15M spends the idle clock and hands the governor back to `max_batch_hours`, which is what this section already says is intended.

What it does **not** move is the per-minute shape Google sees.
`[download].max_requests_per_minute` (48,000, 80% of the 60,000/min approved for the project) is untouched, so only the duration at that rate grows.
Metadata requests are ["available at no charge"](https://developers.google.com/maps/documentation/streetview/metadata) and consume no quota, so wall clock is the only thing being bought — which is also why this knob has no safe-pacing argument to make either way.

`max_cities_per_day` was **left at 40** in the same change, and the reason first recorded here was wrong.
It was held back as the Overpass knob, on an estimate that the 20 → 40 raise had taken nightly Overpass queries from ~20–40 to ~40–80 against the ~100/day guideline.
Counting the network downloads in the per-attempt street logs refutes that: **27 / 20 / 14 / 1 / 27 / 5** on 2026-09-10 through 09-15, because most walks load a frozen network from `data/osm_cache` and only a city's first walk queries Overpass.
Both Overpass refusals in that window (09-13 07:20 and 09-15 02:50) arrived after **1** and **5** downloads — the 09-13 one on that night's first fetch — so our nightly volume is not what tripped them.
The cap is not bounded by Overpass volume at these rates; it was simply not changed here and remains a separate decision.

## Filling an under-full night (issue #404, added 2026-10-01)

**`cycle_days` is a guarantee, not a target, and until #404 nothing spent the capacity a short due slate left idle.**
Measured on prod 2026-10-01 (1,223 enabled cities), every channel had 0 never-collected and 0 overdue cities: 09-30 took 80 of 95 due and 10-01 the last 15, in 4.16 h of a 12 h window.
At a 90-day cycle the steady state is ~15 cities a night, and October is a trough (~7 a night), so the cap and the deadline had stopped being what ended a night.
Cutting `cycle_days` was rejected in the issue: at 45 days 578 cities fall due at once (33,584 Mapillary tiles), which the Mapillary budget drains over 10–15 nights while GSV races ahead un-paired.

**A night's work is now four tiers, in this order, and only the first three are reservations of the cap:**

1. at most `opt_in_cities_per_day` stranded cities (the bounded hoist, #248/#282/#301);
2. at most `refresh_slots` due refreshes in what the hoist left (#308);
3. the rest of the due slate, stalest-first;
4. the **fill**: early refreshes of cities that are not due at all.

The fill composes with the two reservations by construction rather than by argument: they reorder the due slate inside `_collect_due`, and the fill never touches that slate — it runs in `_run_fill` after `_run_city_loop` returns, so the due phase's launch order is identical with the fill on or off (`test_the_fill_composes_with_the_hoist_and_the_refresh_reserve` pins it, with the opening line's `hoisted=`/`promoted` counts).
It runs only when the due loop **ended on its own** — no cap, deadline, SIGTERM or error stop — which is what "fill never outranks a due city" means mechanically.

**The fill ALIGNS a city across every provider it is a member of (Jon, 2026-10-02: "every night we should try to get the same exact cities across all providers").**
A fill city runs **every** channel it is a member of — its enrolled opt-in channels (`kartaview`, `kartaview_streets`, `panoramax`, `panoramax_streets`) included — on tonight's UTC date.
A channel that already succeeded tonight in the due phase is already on that date and is left out, so a city the due phase collected on only some channels is finished by the fill: a late KartaView enrolment, due and hoisted on `kartaview` alone, then gets its gsv and mapillary the same night, and a city due only on mapillary gets its gsv.
The fill never ENROLS anybody (a city not enrolled on KartaView is never run there), and the opt-in channels' own nightly dueness and hoist are untouched.

**Eligibility** (`db.get_fill_candidates`): an enabled city, every member channel of which that did not run tonight has succeeded, at least `[schedule].fill_min_days` ago and under `cycle_days - grace_days` ago, or the city is skipped whole — default and opt-in channels alike.
A never-collected or overdue channel is DUE, and belongs to the due phase: **a late opt-in enrolment is caught up there, through the bounded hoist, and the fill then finishes the city's other channels the same night** ("we will need to play catch up for these providers since we added them late"). A FRESH opt-in channel (under the floor) holds the city back until the whole set can move together.
A consecutive failure since the last success skips the city on a DEFAULT channel; on an OPT-IN channel it drops only that channel from the run, so a failing KartaView cannot freeze the city out of the fill for good, and the night reports it as `realign blocked: kartaview failing (N)`.
Candidates are ordered: **a city the due phase PARTLY collected tonight first** — tomorrow its fresh channel is under the floor, so tonight is its one chance to be aligned — then by the day of the oldest last success among the channels to run, then **misaligned before aligned** among equally stale cities, then `city_id`, so the fill actively re-aligns the catalog; the `Done:` line counts as `realigned` only a city that was misaligned and now has EVERY member channel on tonight's date (not one with a failed run, or a failing opt-in channel left out).
Only an EARLY success is recorded in `early_refreshes` (`_is_early`).
Opt-in channels are priced by their own estimators — KartaView's `max(prior observed, swept-lattice geometry × 1.80)`, Panoramax's z15 tile census — against their own daily budgets and hosts, and they are resumable, so the launch-plan test below applies to them too; KartaView has run up to 3.0× its estimate (Yogyakarta), and a crawl that outruns it pauses and is resumed by the next fill, below.

**Admission is whole-city** (`_fill_judge`, shared by the live path and the dry run): every channel's `_channel_estimate` must fit that channel's daily remainder; per metered host, the **sum** of its channels' prices must fit both the `[hosts.*]` rolling-24h room and the fill's own `fill_host_ceilings` room; the summed `city_timeout_estimate_seconds` must fit the deadline; and every resumable channel must be one `_sweep_launch_plan` would launch with no skip and a cap at least its price.
That last test exists because `est <= room` is not what the launch path asks: a 1-tile Mapillary census against a 4-request remainder fits the room but sits under the launch floor, so the launch skipped Mapillary after GSV had run — the GSV-only refresh the fill exists to prevent.
Each resumable channel is planned against the room AND the clock it would see at its own launch: the host room less the city's earlier channels' prices on that host, and the deadline less their derived needs (exact at one lane, conservative at two).
A walk whose grid sibling runs earlier in the same run is not planned as behind that sibling's checkpoint, since the sibling is admitted uncapped and lands its census first.
The sum matters on the host because the two Mapillary channels share one IP and one pool, and on the clock because a sum is exact at one lane and conservative at two.
A walk whose census its grid sibling is about to buy is still priced at the full census unless the cache already holds it — an over-price, which can only decline a city that would have fit.
**The launch is sized to the fill's room too**: `_run_city_channels(fill_cap=...)` caps a fill channel's request cap (and its non-resumable budget gate) at the fill's own room on the channel's host at that launch — the ceiling and tomorrow's term, re-read after the city's earlier channels — not only at the due-side remainder, which on prod is the 3,000 rolling budget.
Two refusals come before the budget rule, both because the launch path would otherwise collect a subset: a channel whose host refused this machine tonight (read off `HostBreaker.latched`, so no re-check is spent on a city that may not be admitted), and a walk whose street network is not frozen — that walk would query Overpass, a per-IP host with no measured capacity to fill, so **the fill adds no Overpass traffic at all**.
A declined city is skipped and the next one asked, as the due loop does for a budget skip; the ledgers are re-read per admission, so earlier fill cities' spend counts.
The motivating case is in the issue: today a due city whose Mapillary run does not fit still gets its GSV run, which un-pairs its snapshots and strands it behind the hoist (#301, #341); a fill city never does.

**The fill ceiling is lower than the budget on purpose, and it is fixed.**
Prod sets `fill_host_ceilings = { mapillary_tiles = 2260 }` under the 3,000 `[hosts.mapillary_tiles]` budget, every night with no ramp (Jon, 2026-10-02): 2,260 is the highest combined night the #292 window measured clean (`docs/provider-access.md`).
The due slate keeps the whole 3,000, and the fill takes only what is left under 2,260 — so the fill fills unused capacity and never raises any ceiling.
GSV metadata has no per-IP host and no charge, so on gsv only the daily budget and the deadline bound it.

**The fill never borrows tomorrow's due room** (Jon, 2026-10-02).
A rolling 24 h window does not reset at midnight: tonight's fill runs after tonight's due work, so its spend is still inside the window when tomorrow's due slate starts ~24 h after tonight's (tonight's due spend ages out in step with tomorrow's).
So on every metered host with a rolling bound the fill's room has a third term beside the budget's and the ceiling's: the host's cap (its fill ceiling, else its budget) less **tomorrow's projected due demand** there, less what tonight's fill has already spent there (`_fill_host_room`) — the fill may spend at most `cap - reserve` in total.
The demand (`_tomorrow_due_demand`) is read once, as the fill starts: each channel's FULL due list for TOMORROW from the same `get_due_cities` query (its julianday rule, membership and quarantine) — read after tonight's due phase, so what succeeded tonight has left it and what was deferred is still in it — priced with the same `_channel_estimate` (0 already for a census in tonight's cache), with each city's stalest-first rank.
The reserve (`_tomorrow_due_reserve`) is **re-summed per admission, in this order**: drop the CREDITS — the pairs a fill city collected tonight (no longer due tomorrow) and the city being judged (so an 82-day city fits a ceiling of 1.5× its price instead of being declined against its own tomorrow).
Then it cuts each channel's list at `max_cities_per_day` — after the credits, so the cities that move up into tomorrow's window are reserved; finds the cities each GRID channel's daily budget lets tomorrow actually run; prices a paired walk 0 only when its grid sibling is one of those (#290), and in full otherwise; then cuts each channel at its daily budget and each host at its cap, and sum per host (the grid and the walk on the tile CDN are one pool).
It stays conservative where it is not exact: each channel's list is cut at the cap but not at the shared cap or the opt-in reservation, and a resumable crawl is priced whole.
A host with no rolling bound (KartaView and Panoramax on prod) has no figure to subtract a reserve from, and its daily budgets reset at midnight, so tonight cannot borrow them.
The log, the `Done:` line and the dry run say what was reserved: `reserved 812 on mapillary_tiles for tomorrow's due (23 cities)`; the dry run assumes tonight's due slate succeeds.

**A backlog holds its channel** (`_fill_backlog`): any due (city, channel) pair whose `last_attempt_at` predates the batch start was deferred — for budget, a host budget, the deadline, the breaker — or paused, and no fill city that would run that channel is admitted tonight (declined whole as `backlog`), so the fill stops by itself wherever a backlog forms.
Per channel rather than the whole fill, now that the fill runs opt-in channels too: Panoramax is budget-bound on its own most nights, and a Panoramax deferral says nothing about a city with no Panoramax enrolment.
**So while any due KartaView sweep is paused (exit 83 — a multi-night metro), every KartaView-enrolled fill candidate is declined**, which keeps the fill from stacking a second KartaView crawl on the host the paused one is waiting to finish on; the `Done:` line names the hold whatever else the fill did: `holding kartaview (1 due not attempted)`.
A failure records an attempt and is not a backlog.
Nor does a pair no night like this one could run (`_never_fits_tonight`), each logged as such: a non-resumable channel priced over its whole daily budget, one whose need exceeds the whole `max_batch_hours` window (#373), **every other channel of a city whose first grid channel exceeds that window** (the #373 gate defers the whole city with it to keep the pair, so its resumable mapillary is never attempted either), and a walk waiting behind its grid sibling's in-flight checkpoint (the sibling's own pair is the backlog there). Held on those, one oversized city would switch the fill off for good.
`run-due --dry-run` applies the same rule to its preview's hold, so the two agree.

**A fill crawl that pauses is resumed by the next fill, first.**
Admission prices a crawl from its estimate, and a crawl can still outrun it and pause (exit 83). A fill city is not due on that channel, so nothing on the due path resumes it, and its checkpoint is discarded at `CHECKPOINT_MAX_AGE_S` (7 days) with the spend thrown away — while the paused channel's pair is already broken.
So each fill begins with `_fill_resumers`: every city the fill ATTEMPTED inside that age (`fill_attempts`, written at admission whatever the channels then did — keyed on `early_refreshes` instead, a city whose gsv failed while its mapillary paused was never found), with a live checkpoint on a resumable member channel that tonight's due slate does not hold and that has no failure since its last success, nearest the age wall first, and **not held by a backlog** (nothing else will ever resume it).
The resume brings the city back onto ONE date: it runs the paused channels, every member walk deferred behind a paused grid sibling, and — **only when that already covers every metered member channel it could run** — every other member channel that is FREE to re-run (no per-IP metered host: gsv and a frozen gsv_streets walk), so the whole city lands on the resume date.
Without the coverage test a walk paused ALONE would be resumed with gsv beside it and mapillary left on the old date, splitting the very pair it set out to align; such a resume runs the paused walk alone.
When the realigning run does not fit tonight, the paused crawl (with its walk) is resumed alone, because the checkpoint is what expires.
`fill_attempts` is pruned to the same horizon (the checkpoint age wall plus a day) as each fill starts.
**What can stay unaligned, and why:** a channel on a per-IP host that already succeeded on the first night (a KartaView or Panoramax census beside a paused Mapillary one) is not re-paid to realign one night, so it keeps the first night's date; and so does gsv when the realigning run did not fit. The pause itself is what the launch-plan admission test exists to make rare — it admits only a crawl the plan would run uncapped at its estimate — so what remains is a crawl that outran its estimate (KartaView has run up to 3.0×).
A resume that lands is marked an early refresh against the success the channel still had; the pause line in the log says the channel is not due and the next fill resumes it, rather than "stays due".

**A failing city leaves the fill.** A candidate needs no consecutive failure since its last success on any DEFAULT channel (a failing opt-in channel is dropped from its run instead, above), so the fill can add at most one failure per channel between successes and can never be what quarantines a city that was never due.

**Walks stranded inside the fill** (a host latched, or went busy, between a fill city's grid and its walk) are retried once by the fill's own `_retry_stranded_walks(only=...)` pass — `only` is load-bearing: the due phase's strandings were already retried, and their cities are not in the fill's city map — and that pass launches as a fill launch (`fill=True`, and the fill's `fill_cap`, so a retried walk is sized to the fill's room too).
One still stranded is recorded in `HostBreaker.fill_stranded` (in a `finally`, so a fill that raises still books it) and gets its own alert paragraph, with **no** recovery command: the walk is not due, so `run-due --city` would skip it, and nothing is lost — it keeps its previous success and comes due on its own clock (the date is printed), or is refreshed with its city by a later fill.
The `Done:` line says `(N in the fill, not lost)` beside the STRANDED count, and the alert SUBJECT's "STRANDED un-walked" counts only the due phase's; a fill that raises is named `FILL CRASHED`, not `LOOP CRASHED`.

**It counts toward `max_cities_per_day`**, which stays a ceiling on the whole night; at prod's 80 the cap, a channel budget, the fill ceiling or the deadline ends a filled night, whichever comes first.
**It never runs on an operator-narrowed run** — `--provider` would fill a subset of each city's channels, `--city` names exactly what the operator wants, and `--limit` is a catch-up's cap — so those runs are exactly what they were.
`fill_min_days` unset (the repo default) means no fill and no change to any log line; a bad value, or a bad ceiling, warns and turns the fill **off** (fail-closed is off here, since the fill only adds traffic — unlike `[hosts.*]`, where dropping a bad entry would fail open and the channel-running commands refuse instead).

**What a night says about it.**
The opening line carries `fill_min_days=N` when set.
The `Done:` line counts the cities apart — `across 23 cities (15 due, 8 fill)` — and adds `fill (early refresh, >= 30 d): 8 cities, 3 realigned, 24/24 runs, 24 early refresh(es) recorded; declined 3 for mapillary_tiles; reserved 812 on mapillary_tiles for tomorrow's due (23 cities); ended by budget (mapillary_tiles)`.
`ended by` is one of `city cap (N)`, `deadline (N h)`, `budget (<channel or host>)` (the candidates ran out with the last ones declined for it), `candidates exhausted`, `unfrozen street network`, `host refused tonight`, `held: backlog (...)` (the last ones were declined for a held channel), `received SIGTERM`, or `unexpected error in the fill phase`; a night that never reached it says `fill not reached`, and a narrowed run says `fill off for --limit`.
An error inside the fill is logged under its own wording (not the city loop's), ends the fill, and makes the night unhealthy, but the tail still publishes (#167).
`scripts/night_length_analyze.py` parses the `(D due, F fill)` split and counts a night that ran fill cities as its own `filled` population, never pooled into `full`: its length is set by the fill's end, not by the due work.
`run-due --dry-run` previews it through the same `_fill_judge` against its simulated ledgers, listing what the budgets admit up to the cap — including the runs that FINISH a partly due city, since it assumes tonight's due slate succeeds; the deadline is the one term a preview cannot price, so it says the deadline decides how many run.

**The series records it.**
Each fill channel that succeeds is written to `early_refreshes` (catalog v18, keyed by scheduler channel, with a walk's network type; `docs/architecture.md`) with its prior success, so a shortened interval is a fact in the catalog rather than something inferred from run dates, and the aggregate marks a GRID run `"early_refresh": true` (walk marks are recorded, not yet published).
Success is read off `schedule_state.last_success_at` having moved since admission, so a skipped or failed channel is never marked.

Not done here: an operator-facing `status` view of fill eligibility, publishing the mark for walks, and any change to Panoramax pacing (#405).
The provider forums were not re-read for this change: it raises no rate, no budget and no ceiling, and its one new per-IP number (2,260) is a figure this project already measured clean — but it DOES call KartaView and Panoramax more often for their enrolled cities (inside their unchanged daily budgets), which CLAUDE.md's READ THIS FIRST rule asks to be checked against those providers' forums before deploying.

## The failure quarantine, made visible (issue #421, added 2026-10-02, revised 2026-10-05 for #424)

**A (city, channel) at `[schedule].max_consecutive_failures` (5 on prod) is dropped from `get_due_cities`, and nothing but a success lifts it.**
A dropped pair is never attempted, so it never fails, so it never alerts: prod alerts on every failed collection (`failure_threshold = 1`), so nights 1–5 each sent an email and night 6 onward was silence, which reads as the problem having gone away.
The fill (#404) made that sharper: it drops a failing opt-in channel and refreshes the rest of the city, logging "realign blocked", so a city can sit indefinitely with one provider on an old date and the quarantine is the only durable signal.

**The set is `db.get_quarantined`**: an enabled city, a member of the channel (`COALESCE(member, default)`, as dueness reads it), and `consecutive_failures >= max_consecutive_failures`.
Staleness is deliberately not part of it, so the count tracks the set an operator has to clear rather than flickering with the cycle; a disabled city or a non-member is left out, because neither can be due whatever its counter says.

**The alert is claimed, once per failure streak, inside the night email (#424).**
`schedule_state.quarantine_alerted_at` (schema v21) is the UTC instant a night claimed the right to email the pair's current failure streak; NULL means it has not been emailed.
After the fill (and after the dry-run return, so a preview stamps nothing), `cmd_run_due` calls `db.claim_quarantine_alerts` over every enabled channel: each quarantined pair with a NULL stamp is stamped by a compare-and-set `UPDATE ... WHERE quarantine_alerted_at IS NULL AND consecutive_failures >= cap`, and the pair is emailed iff that UPDATE's rowcount is 1.
The decision and the stamp are one statement, so two overlapping `run-due`s cannot both email a pair: the second UPDATE waits on the write lock, finds the stamp, and matches nothing.
It is not `BEGIN IMMEDIATE` + SELECT + UPDATE, because an explicit `BEGIN` raises inside an already-open implicit transaction, and this runs in the tail.
It is never `count >= max`, which would re-alert every night forever, and a failure never clears the stamp, so a pair some other path pushes from 5 to 6 stays silent.
Stamp, then email: the claim commits before `_finish_batch` sends, so a crash between the two loses that one email (at-most-once) rather than sending it twice; the `Done:` count still carries the pair, and the unit's `OnFailure=` email reports the crash.
The claimed rows reach `_finish_batch` as `quarantine_alerts`: an `N QUARANTINED` subject part (the subject is what gets read at 03:00) and a paragraph naming each pair's count, `last_error` and its pasteable `reset-failures ... --execute` command, `--config` included.
It is part of `unhealthy`, so it alerts regardless of `failure_threshold` — it is that streak's only email.
It rarely changes a night's exit status, since the failure that tripped it usually made `attempted > succeeded` in the same process — but not never: a pair that crossed the cap between nights, or one an overlapping `run-due` pushed over, is claimed by a night whose own collections may all have succeeded, and that night exits 1.
A separate email was rejected (the night email already carries every other condition), as was a per-night "realign blocked" email, which would duplicate the per-failure alert.

**The standing set is counted on every `Done:` line while it is nonempty** — `; quarantined: 3 (kartaview 2, panoramax 1, 1 alerted tonight)` — over every enabled channel, not a filtered night's, so a `--provider mapillary` catch-up never reports a KartaView quarantine as gone.
`status` marks each quarantined pair `QUARANTINED` in its failing-pairs list, from the same query, and prints the count with the clear command.

**Both steps are guarded**: the claim (`_claim_quarantine_alerts`) and then the standing-set read (`_quarantine_snapshot`), both between the fill and `_finish_batch`, so an unguarded raise in either would cost the whole tail — aggregate, manifests, backup, publish and the alert — for a reporting step.
A raise is logged, named on the `Done:` line as `; quarantine alert claim FAILED (<error>)` or `; quarantine check FAILED (<error>)` — with any `;` in `<error>` turned into `,`, because the `Done:` line is split on `;` (`scripts/night_length_analyze.py`) and the clause's leading separator must be its only one — and alerts on its own as a `QUARANTINE CHECK FAILED` subject part.
A failed claim rolls back its stamps, so nothing is lost: the next night claims the same pairs.
That holds for a raise after some UPDATEs have already stamped, too: the claim calls `conn.rollback()` before the exception propagates, because the connection is shared with `_finish_batch`, whose `prune_host_usage` commit would otherwise persist those stamps for pairs that were never emailed (PR #435 review).
Any write the caller left pending is committed on entry, so the rollback discards only the claim's own stamps.
A failed read drops that night's standing count but not the email, because the claim runs first.

**What the stamp catches, and what it still does not**, named rather than argued away.
The claim's gates (enabled, member, at the cap) are evaluated at claim time, so a pair that enters the set BETWEEN nights is emailed the first night after.
`assess-city` is not one of those paths — it runs with `record_failures=False`, so a manual probe never increments `consecutive_failures` at all — but these are:
a LOWERED `max_consecutive_failures`, which sweeps every pair between the old and new caps in at once;
a re-enabled city whose counter was already at the cap;
a re-enrolled channel (`enroll-city --clear`, or a bare enrol on an opt-in channel) over a row whose counter was already at the cap;
and enabling a `[providers.X]` block, which brings that channel's at-cap rows into the claim at once.
Still not caught:
once a stamp commits, that streak's one email can still be lost — a crash anywhere in the tail before the send (aggregate, manifests, backup, publish), a `send_alert` that fails (it returns False and never raises), or `[alerts].enabled = false`; each claimed pair is therefore logged by name at WARNING the moment it is stamped (`Quarantine alert claimed: <city> [<channel>] ...`), so the scheduler log, whose tail `notify-failure` emails, records which streaks were consumed, and the `Done:` count still carries them;
the same streak re-reaching a RAISED cap is not emailed again, since only a success or a reset clears the stamp (each extra failed night still emails on prod, where `failure_threshold = 1`);
and the first night after the v21 deploy emails every standing quarantined pair once, by design, since the migration backfills nothing and a pair quarantined before #423 shipped was never emailed at all.

**The amnestied exit-code families can never reach it.**
Blocked (75/76/81/84), busy (79/80/82/85), crawl-incomplete (83) and argv-rejected (2) record no `consecutive_failure`, and neither does a child killed by the SIGTERM wind-down (#206), so a host block or a `systemctl stop` never quarantines the city it stopped; `test_an_amnestied_outcome_never_quarantines_a_pair_one_failure_from_the_cap` pins each against a plain failure that does.

**`reset-failures CITY --channel C [--execute]`** is the way out, so the alert's fix is a command rather than SQL against the live catalog.
It is DRY-RUN until `--execute`, and the preview prints the count, whether the pair is quarantined, and its `last_error`, so the cause is read before it is cleared.
It moves the counter and clears the alert stamp, so a re-quarantine emails again; `last_success_at` stays (a reset is not a success, and stamping one would push the next attempt a whole cycle away), and so does `last_error` (the only record of why, overwritten by the next attempt anyway).
An unknown channel, an unresolvable city and a pair with nothing to reset all exit 64 writing nothing — the last because a reset that changes nothing and exits 0 is the silent no-op `enroll-city --clear` refuses for the same reason.
A disabled city or a non-member is allowed and noted, since the counter still gates the pair the moment it is enabled or enrolled.
The preview also prints the alert stamp (`alerted:`).
`enroll-city` over an at-cap row prints the pair's quarantine state and whether it was already alerted, and never touches the stamp: the stamp means "this streak was emailed", and enrolling does not change the streak.
It promises "the next run-due emails it once" only when the claim could take the pair — the city enabled, a member of the channel after the command, and the channel configured; after `--remove`, on a disabled city or on an unconfigured channel it says the pair is emailed the first night all three hold instead.
Fix the cause first: a cleared pair that still fails is quarantined again after five more nights, and alerts again then.

## The subcommand roster, and the production config (added 2026-08-25)

Written 2026-08-25, when the CLAUDE.md rewrite turned its command cheatsheet into a table and two subcommands turned out to be documented nowhere.

**`assign`** (re)computes the `day_of_cycle` stagger for every enabled (city, provider) over `[schedule].cycle_days`, via `db.assign_schedule` — the rebalance handle after registering or enabling cities in bulk, so the nightly slate stays level rather than front-loaded.
It writes `day_of_cycle` and nothing else, which is what keeps the nightly `assign` (`run-due` calls it before every night) from un-enrolling every opted-in pair; a pinning test says so.
Assignment is **not** enrolment: on an opt-in channel it creates a row per enabled city and leaves `member` NULL, so the channel gains ~1,144 rows that collect nothing.
`status` and `assign` therefore print a per-channel enrolled count for each opt-in channel — without it, a table of blank `DUE` cells reads as "the flip did not take".

**`enroll-city CITY --channel CHANNEL [--remove | --clear] [--list [--excluded]]`** is the operator handle for `schedule_state.member` (#248), and it exists because hand-SQL has four ways to be a silent no-op here: `day_of_cycle` is `NOT NULL` with no default so a bare `INSERT` fails; an `UPDATE` matches zero rows and exits 0 whenever `assign` has not yet run with the channel enabled; a typo'd slug is the same zero-row success; and NULL/0/1 is three-valued with its meaning in a code-side table.
It refuses (`USAGE_EXIT_CODE`, changing no row) an unknown channel, an unresolvable city, and — **in the enrol direction only** — a **default-membership** channel or a city with `cities.enabled = 0`.
Both of those two are scoped to enrolment because enrolment is where they are no-ops: every enabled city is already a member of `gsv`, and a disabled city can never be due on anything.
**Exclusion is a real write on any channel** (`--remove` writes `member = 0`, `--clear` restores NULL), and it is what makes a city collectable on one channel and not another — the handle that a purposive batch needs when `cities.enabled` would drag three more channels along with it.
The original refusal objected to *invisibility* rather than to the operation ("a second less visible way to disable a city is how two operators disagree about why it stopped"), so allowing it is paid for on the visibility side: `status` prints `excluded` rather than `not enrolled` for an explicit `0` on an ENABLED city — a disabled city still prints `no` on every channel, since `cities.enabled = 0` short-circuits the per-channel column, so a pre-set exclusion is visible in the footer's count rather than in its own row — and the membership footer prints a line for any default-membership channel carrying one.
**Pre-setting an exclusion on a still-disabled city is supported and is the correct rollout order**, since a newly enabled city has no `schedule_state` rows and therefore leads the stalest-due ordering on its first night — enable-then-exclude races the 02:00 timer for a whole city's collection, and for a 40 km-clamped city that race costs 4M grid points.
**`--all` stays refused on a default-membership channel in every direction**: its blast radius is the whole catalog, `--all --remove --channel gsv` is one keystroke from the `kartaview` form, and its cheapest-first ordering is a KartaView sweep-cost rationale that means nothing for a GSV grid.
It deliberately does **not** refuse while the channel is still unwired or unconfigured — enrolment must precede the config block or the rollout order is impossible — and prints a `NOTE` saying nothing collects it yet.
`--remove` writes an explicit `0` and `--clear` restores NULL; the two are indistinguishable to dueness today and kept apart because only the explicit `0` survives a future flip of the channel default.
`--list` is scoped differently from the rest because it is read-only: it accepts a **default-membership** channel too, and it refuses to run beside `--remove`/`--clear`, which argparse's mutually exclusive group does not cover and which would otherwise be accepted, ignored and exit 0.
`--list --excluded` inverts it to the **explicit zeroes**, and it is the only way to enumerate them — `status` has no city filter and prints the whole catalog, while a plain `--list` on `gsv` prints every member and says nothing about who is missing.
It lists explicit zeroes rather than everyone the membership clause omits, because on an opt-in channel those are different sets and only the first records a decision somebody made; and unlike the membership listing it does **not** filter on `cities.enabled`, since a pre-set exclusion on a not-yet-enabled city is exactly the row an operator staging a rollout needs to see.
A **single-channel city is now expressible** — register it, exclude it from the channels it should not join, then enable it — which is what retires the old foreclosure that a kartaview-only city was impossible because `enabled = 1` made it a member of all four default channels.
What is still not expressible is a *per-channel enable date*: exclusion is a switch, not a schedule, so staging a batch across nights is a sequence of operator commands rather than a property of the catalog.

**The rollout order, and it verifies BEFORE the point of no return:**

1. `enroll-city CITY --channel gsv --remove` and the same for `gsv_streets`, while the city is still **disabled**.
2. `enroll-city --channel gsv --list --excluded` — confirm every city you meant is flagged `city disabled, exclusion pre-set`. A mistyped slug exits 64 and writes nothing, so this is the step that catches it, and it has to happen while the city still collects nothing.
3. `enable-city CITY --no-opt-in` (#374) — `--no-opt-in` because the point of this order is that the city joins ONLY the channels it was not excluded from; without it the opt-in pairs are enrolled behind their gates (`docs/operations.md`).
4. `run-due --dry-run` to confirm no `gsv` lines before the 02:00 timer fires.

Doing (3) before (2) leaves a mistyped or forgotten exclusion on an ENABLED city, exposed to the next timer — which for a 40 km-clamped city is 4M grid points, most of a night.
Expect the newly enabled cities to arrive through the stranded reservation rather than all at once, since they are not due on gsv and therefore never lead the union.
They get a **share** of `[schedule].opt_in_cities_per_day`, not all of it: the reservation round-robins across stranded populations, so with a KartaView widening and a transiently-stalled population also in flight the share is about a third of it (measured on a prod-shaped slate: 4 of 10).
Raise the key for the duration of a rollout rather than expecting ten cities on night one.

**Seeding an opt-in channel: DUE IMMEDIATELY is not REACHABLE TONIGHT, and the gap can be weeks.**
Dueness is per-(city, channel), so a fresh `schedule_state` row has `last_success_at` NULL and the city is due on the next run regardless of any sibling channel's 90-day clock.
That is the whole of the eligibility question and none of the reachability one — the #328 lesson, arrived at from the enrolment side instead of the exclusion side.
A newly enrolled city is stranded (not due on rank 0), so it enters the bounded hoist, competing for `opt_in_cities_per_day` (10 on prod) against every other stranded city: mapillary-stranded cities, and every never-collected KartaView-enrolled city from the #282 widening — ~380 of the 502 enrolled on 2026-09-03 still had a NULL `last_success_at`.
Ordering inside the reservation is live-checkpoint first, then union order, then `city_id`, so a seed city with an unlucky slug can sit behind hundreds of them.
**So do not wait for the nightly batch to prove a new channel works.** Run it filtered, which makes the new channel rank 0 and therefore strands nobody:

```
scheduler enroll-city "<city>" --channel panoramax
scheduler enroll-city "<city>" --channel panoramax_streets    # nearly free when paired
scheduler run-due --dry-run                                   # prices both, names each child's cap
scheduler run-due --provider panoramax,panoramax_streets --limit 5
```

The same recipe is how a KartaView tranche is exercised, and it is the only supported bulk path either way — never a detached script.
The filtered run advances only those channels' clocks, which for a first collection costs nothing (there is no paired snapshot to un-pair yet).

**`notify-failure`** emails the recent scheduler-log tail and is wired as the unit's `OnFailure=` hook (`deploy/systemd/streetscape-tracker-notify@.service`), so a crash that never reaches the in-run alerting still produces an email.
It exits 0 when it alerted (or alerting is intentionally off) and 1 only when a send was attempted and failed, so the notify unit's own status is meaningful.
`run-due` returns nonzero on any failed city, so this hook can double-report a failure the in-run threshold alert already covered — accepted, since the alternative is a class of silent nights.

**`screen-provider PROVIDER`** (#316) is the standing growth screen, and it is the one scheduled thing here that is **not** part of a night.
It re-asks over the whole catalog whether a provider has any imagery in each city yet, reading a coarse count layer where 113 requests answer all 1,144 enabled cities, and writes one dated `provider_screen` row each.
Panoramax is why it exists: its US corpora are months old rather than decades, so there is no archive to backfill and a city's growth is observable only if we were already watching when the imagery landed.

It runs on its own weekly timer (`deploy/systemd/streetscape-screen-provider.timer`, Mondays at 18:00 Pacific, after a full 12 h night's latest end) rather than as a `run-due` tail step, for two reasons that both matter: the question has a different cadence from the nightly slate, and the batch's wall clock is already the binding constraint (#304).
The timer is deliberately far from 02:00 because both take the same machine-wide Panoramax host lock — an overlap is not a race but a screen that exits **85** and records nothing that week.

Three properties are load-bearing and easy to erode:

- **Every number it records is an upper bound**, summed over every map cell that overlaps the city's bbox, each counted whole — so a cell straddling the edge contributes imagery from outside it, and the bbox is a rectangle, not the city. A zero is conclusive; a positive number means only "look closer".
  The cell is **measured, not assumed**: each pass decodes the H3 resolution of every hexagon id it read into `provider_screen_cells` (v19), and the artifact's `cell` describes the latest pass from that (#406 found resolution 7 where the published constant said 6).
  A coarser, mixed or non-H3 layer is warned about and recorded, never refused — overlap selection and whole-cell counting keep the bound sound at any resolution a z6 tile can still draw, so a refusal would cost an un-backfillable week to protect a number the artifact can describe instead.
  FINER than `MAX_SCREEN_H3_RESOLUTION` (9) is refused unless `--allow-fine-cells` is given: a res-10 hexagon's average edge (75.9 m) is under half of one z6 tile unit at the equator (~152.9 m = 40,075 km / 64 / 4,096), so it can quantize to nothing and a zero can be false. The column names say `upper_bound` for that reason, and the published artifact carries the caveat as a field.
- **`--limit` is `--measure`-only.** A screen is a whole-catalog observation: screening a subset would write a dated row for some cities and not others, and the published series' "cities positive on this date" would then count two different observations on one axis. The exact z14 measure is what needs bounding (~51,000 tiles for every positive city), and it prints rather than writing, so one column never means two instruments.
- **Three refusals, all before the write, and they are not redundant.** Every tile answering 204 is a meta-catalog serving nothing (an empty tile answers 204, but 201 of 235 world z6 tiles hold imagery), and a single 404 ends the pass, since the host never 404s a tile route and an unread z6 tile is every city under it (#407).
  Tiles that answer *with a body* from which not one hexagon decodes is a renamed LAYER — a structural check that needs no history, which is what makes it the one that protects a first run.
  And a pass where every city reads zero although hexagons decoded fine, in a catalog that has screened positive before, is a renamed COUNTER; only that last one needs history, which is precisely why it cannot be the only check.
  A city whose frozen bbox maps to no tile at all is refused too, rather than recorded as a zero nobody measured.
  `--allow-collapse` overrides the two collapse checks once an operator has verified the endpoint by hand; nothing overrides the empty-tile guard or the 404 stop, which have no honest reading.

The command writes and publishes `provider_screen.json.gz` itself, and the nightly tail deliberately does not rebuild it — nothing else changes its inputs, so a nightly rebuild would add a failure surface for a file that cannot have moved.
`regenerate-aggregate` does rebuild it, because that command is the prescribed recovery from a stale published set and has to cover every published file.

**Production reads `config/scheduler.makelab1.toml`, not `config/scheduler.toml`** (passed via `--config`; the filename is historical — the service itself runs on makelab2, guarded by `ConditionHost=makelab2*`).
The two diverge materially — budgets, absolute paths, `[publish].enabled`/`[publish].local` — so an operational change edited only into the repo default changes nothing in production, and vice versa: keep any comment-level rationale in step across both files.

## The user timers after a reboot (issue #369, added 2026-09-26)

**What was measured.**
On 2026-09-23 makelab2 hung mid-night (the scheduler log stops at 06:18 PDT, load 165.5, before the tail, so nothing published) and rebooted at 08:15 PDT; the lingering user manager `user@29497.service` entered active at 08:16:09, and `autofs.service` at 08:16:14.
Afterwards all four user timers — `streetscape-backup-check`, `streetscape-prefreeze`, `streetscape-screen-provider`, `streetscape-tracker` — read `enabled` but `inactive`, while `loginctl` reported `Linger=yes` and `systemctl --user is-system-running` reported `running`.
Nothing collected or published until the operator ran `daemon-reload` and `start` by hand, and nothing alerted, because the only watchdog (`backup-status --alert`, #193) runs from one of those four timers.

**The leading cause, unproven.**
The user manager scanned `~/.config/systemd/user/` on the NFS home five seconds before autofs mounted it, found no unit files, and never looked again.
It cannot be proven from here — `journalctl` for the manager is unreadable by `jonf` — and it cannot be fixed from here either: ordering `user@.service` after the mount is a root-side drop-in (`RequiresMountsFor=/homes/gws/jonf`), an open ask for CSE IT, alongside whose reboot it was.

**The design: a user crontab on local disk.**
`deploy/cron/streetscape-tracker.crontab` runs `scheduler timer-status --rearm --alert` at `@reboot` and daily at 08:30 Pacific.
A user crontab lives in `/var/spool/cron` on local disk, so it does not depend on the NFS home being mounted when the user manager starts; whether makelab2's `crond.service` orders after `autofs` is unverified, and the `@reboot` line does not rely on it: it polls (`--wait-s 1800`, every `--poll-s` 15 s) until BOTH `systemctl --user is-system-running` answers AND at least one shipped `.timer` file `isfile()`s under `~/.config/systemd/user/`, which is also what triggers the autofs mount.
Any, not every: an unmounted home hides every file at once, while a timer shipped but never installed hides one — under "every", that one file blocked the re-arm of all the installed timers and was misreported as `UNIT FILES UNREACHABLE`; now it reaches the per-timer check and is reported by name as `NOT INSTALLED`.
The checkout, the venv and `logs/` are on makelab2's local ZFS pool; the only NFS dependency is the unit files, which is the thing being waited for.
The check is cause-agnostic on purpose: whatever left a timer enabled-but-inactive — this race, a lost linger, an operator `stop`, a future systemd change — the repair is the one done by hand on 2026-09-23, and the alert fires either way.

The set of timers is read from the **shipped** `deploy/systemd/*.timer` files, never a second list, and the active host from the collection unit's `ConditionHost=`: on any other host (makelab1 shares the NFS home but has no linger) the command does nothing.
`XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS` are filled in only when unset, since cron's environment has neither and every `systemctl --user` call then fails `Failed to connect to bus`.

| A timer that is… | Reported as | With `--rearm` | Healthy? |
|---|---|---|---|
| loaded and `active` | `active` | left alone | yes |
| `UnitFileState` `disabled`, `masked` or `masked-runtime` | `paused` | left alone — a deliberate operator pause | yes |
| loaded, enabled, not `active` (the #369 signature) | `INACTIVE` | one `daemon-reload`, then `start` | only if the re-read state is `active` |
| `LoadState` not `loaded` | `NOT INSTALLED` | the same `daemon-reload` (the race leaves exactly this), then `start` | only if the re-read state is `active` |

`daemon-reload` is needed because the manager may hold the empty unit map of the scan that raced the mount, and `start` is needed because `daemon-reload` starts nothing and `timers.target` is already active, so its wants are not re-pulled.
A `start` that returned 0 is never trusted; only the state re-read afterwards counts.
Exit status is 0 when every non-paused timer is active at the end — a successful re-arm included — and 1 otherwise; `--alert` never changes it.
`--alert` mails when the verdict is unhealthy **or** anything was re-armed: a re-arm is healthy but not silent, because it means a reboot (or similar) happened and the night before it may sit unpublished, which on 2026-09-23 needed a hand-run `regenerate-aggregate --publish`.

**The crossed heartbeat.**
Every run that reaches the check — the give-up path included — writes `logs/timer_watchdog_status.json` atomically, and `backup-status` goes unhealthy when it is missing or older than `[schedule].timer_watchdog_max_age_h` (0 = off, the code default; 48 on prod).
So the two watchdogs cover each other: cron catches dead timers, the noon backup-check timer catches a dead cron.
The heartbeat measures whether **cron** runs, not whether the timers do (the watchdog alerts on those itself); a hand run refreshes it too, so a dead cron is reported 48 h after the last run of either kind.
**The honest gap:** if crond stops running user jobs AND the timers are dead, nothing alerts.
Only an off-host monitor or a root-side mechanism closes that, and neither is in scope.

**The pause verb changed.**
Because the daily line starts any enabled-but-inactive timer, the `stop` verb on a `.timer` is now a pause of at most a day.
The documented pause is `systemctl --user disable --now <x>.timer` (resume: `enable --now`), which the watchdog reports as `paused` and leaves alone; `tests/test_timer_watchdog_crontab.py` refuses the old spelling in any doc.
`stop` on a `.service` is unaffected.

**`Persistent=` interaction.**
A re-arm after a missed 02:00 fires the nightly batch at once, as the timer itself would at boot — intended.
The 08:30 slot is chosen so that catch-up night (the 15 min randomized delay, `max_batch_hours`, and the unit's `TimeoutStopSec` as the tail's stand-in) still ends before the next 02:00, and so that it precedes the noon backup-check timer.
`streetscape-prefreeze.timer` stays `Persistent=false`, so a re-arm never fires a missed afternoon pass.

**Alternatives rejected.**

- A bare `@reboot systemctl --user daemon-reload && start …` line: not self-verifying (a disallowed crontab, a slow manager or a lost linger says nothing), and it would restart a timer an operator paused.
- A root drop-in for `user@.service`: the real fix, but not ours to deploy; this mechanism keeps working after CSE IT does it.
- A system crontab or system timer: root.
- An off-host freshness monitor on `cities.json.gz`: detection only, no re-arm, and it needs a second host somebody operates.
- Moving the unit files off NFS: every user-unit path is under `~`, and a local `$XDG_CONFIG_DIRS` for the manager needs root.
- Re-arming from `run-due` or its tail: shares the timers' fate by construction — the #193 lesson again.
- Restarting `timers.target`: stops every timer of the user, a paused one included; a per-timer `start` is what lets a disabled timer stay paused.

**Unmeasured.**
The 30-minute `@reboot` wait budget (generous against the 5 s gap measured, and against a slow ZFS import) and the 48 h heartbeat gate (one missed daily run, like `STALE_AFTER_HOURS`) are judgement numbers.
