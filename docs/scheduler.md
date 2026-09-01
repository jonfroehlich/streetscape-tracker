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

**Every channel is paced, so every channel's per-city timeout is DERIVED rather than flat, and `city_timeout_minutes` (180) is only the floor.**
The shape is the same for all of them — `estimated_requests / (rate × achieved_rate_fraction) × _TIMEOUT_HEADROOM + _TIMEOUT_FIXED_SLACK_S`, never below the floor — and what differs is where the request count and the rate come from: gsv and `gsv_streets` off grid points and on-street samples against `[download].max_requests_per_minute`, the two Mapillary channels off the shared z14 tile count, and the two KartaView channels off their bbox's swept circle count at 16/min (#238, #258).
Both census pairs price their walk off the **bbox**, never the sample count: a KartaView walk of Krabi is 64 estimated circles, not its 18,851 on-street samples, and the derived timeout is deliberately blind to whether the census is already cached — a walk that finds no reusable one must still be given time to fetch it.
The reason it is derived at all is that **a SIGKILLed child records no `api_usage`** — every `db.add_api_usage` call lives in the child, after the download returns — so a timeout that fires mid-run loses the whole spend from the daily ledger *and* burns one of the five `consecutive_failures` that nothing but a success resets.
**`_channel_estimate` reads the census cache and `estimate_requests` deliberately does not (#290).**
The first is what the budget gates (`est > budget`, `used + est > budget`) and the dry-run listing read, and it returns 0 when a probe finds a reusable census for that channel's provider — without which the cheapest channel of the night, a walk whose census the grid run bought minutes earlier, is exactly the one a nearly-spent budget defers.
The second also feeds the timeout derivations below, where a 0 would collapse a child's timeout onto the flat floor; and since a probe is marker-only, a hit is a strong hint rather than a promise — narrowed two ways, by comparing the marker's recorded store format against this build's and by probing with the window less `max_batch_hours`, so a format bump or a mid-batch expiry is a miss rather than a free-priced fetch — and the child may still fetch for real.
The `achieved_rate_fraction` differs per channel because what falls short of the configured rate differs: gsv uses **0.5** for the async engine's structural undershoot against a project quota it never approaches, Mapillary **0.8** because its limiter is a hard ceiling the concurrent fetch tracks closely, and KartaView **0.5** because its walk is *serial*, so per-request latency cannot hide behind other requests in flight and at 16/min the 3.75 s interval is genuinely comparable to the latency of a 2,000-record page.
Derived values are then clamped to what is left of the batch deadline (never below `_MIN_CLAMPED_TIMEOUT_S`, 300 s), which is what bounds a metro KartaView sweep whose honest timeout exceeds `max_batch_hours` outright — acceptable only because #239's checkpoint turns that kill into a resume rather than a discarded night.
**But a kill is a resume of the WORK and not of the SCHEDULE, and that is where the acceptance runs out.**
A *deliberate* pause exits `SWEEP_INCOMPLETE_EXIT_CODE` (83) and is amnestied in `_run_city_channels` beside the blocked- and busy-host conditions, so it can repeat indefinitely; a SIGKILL has no exit code, nothing can tell one that checkpointed progress from one that made none, and it counts a `consecutive_failure` that only a success resets — so five clamped nights quarantine the city for a 90-day cycle.
A metro sweep that cannot finish inside five nights therefore needs #248's per-(city, provider) dueness, not a larger timeout constant.
That budget of five is survivable only if the five nights are **consecutive**, so the checkpointed progress accumulates — and consecutive is what `_collect_due`'s hoist buys.
Note which of the two unfinished-sweep arms a nightly batch takes, because since #273 it is usually the **pause**, not the SIGKILL.
`_run_one_city` hands every sweep the night's remaining budget as `--kartaview-max-requests`, so a sweep that runs out of budget stops itself deliberately: exit 83, amnestied, no `consecutive_failure`, no city-cap slot.
What bounds a paused city is therefore not the five failures but `CHECKPOINT_MAX_AGE_S` — seven days from the checkpoint's **first** commit, after which its rows would be spliced into a snapshot dated today and it is discarded.
The SIGKILL arm is still reachable, because a sweep can run out of wall clock before it runs out of budget.
That one does count a `consecutive_failure` and does consume a slot, which is why the five-night bound still exists for it and why the hoist has to put tomorrow's retry in the *first* slot rather than merely in the list.
The union of the per-channel due lists is ordered by first appearance, so `gsv` (rank 0) dictates city order; a city whose `gsv` run succeeded but whose sweep paused sits at the tail of ~949 cities and is truncated by `max_cities_per_day`, returning months later rather than tomorrow.
The hoist moves a city to the head of the slate when **every** channel it is due on is opt-in — `all`, not `any`, so a city due on `gsv` too keeps its exact union position and `gsv`'s stalest-first ordering is strictly untouched.
It reorders the **city list only**, never the union loop, because `providers_for_city` is passed straight to `_run_city_channels` where `pending = list(providers)` *is* the launch order.

**The tail — aggregate, streetwalk manifest, catalog backup, publish — is what makes a night visible, and it only runs if the city loop returns**, so every way of ending the loop goes through `_run_city_loop`,
which always returns counters instead of propagating: a `[schedule].max_batch_hours` deadline (10 h) stops *starting* cities and clamps the in-flight child's timeout to what's left,
a SIGTERM handler turns systemd's stop into a wind-down request checked between cities *and between a city's channels*, and an unexpected exception is logged, published anyway, and then reported as an unhealthy night (nonzero exit + alert, so publishing can't hide a bug).
**The tail also prunes the shared census cache** (`prune_census_cache`, #290), beside the backup and the publish and with the same best-effort posture — it swallows its own filesystem errors, because the prune is housekeeping and the publish and the alert come after it.
That prune is the only thing bounding the cache's size: an entry is written for every census a night fetches and is not overwritten until that city comes round again, ~80 days later.
**The tail's own index rebuilds carry that same posture** — each of the three (`generate_aggregate_v2`, `generate_streetwalk_manifest`, `generate_driving_plan_summary`) runs through `_tail_artifact`, which reports a crash (alert naming the index + nonzero exit) instead of propagating it, so a broken index can't cost the catalog backup and the publish that follow.
Safe because all three write via `_write_json_gz_atomic`, leaving the *previous* good file in place: the publish ships a stale-but-valid index, never a truncated one.
This gap was real, not theoretical — on 2026-08-17 a manual catch-up piped to a reader that had gone away (`run-due ... | tail -40`) collected 10/10 cities and published none of them, because `tqdm`'s `status_printer` flushes the **raw** `sys.stderr` outside its own `DisableOnWriteError` guard and the resulting `BrokenPipeError` took out the whole tail.
**A dead output pipe is therefore treated as an ordinary condition in four separate places, because any one of them alone still loses the night.** (1) **Every progress bar in the repo goes through `progress()`** (`progress.py`) and never a bare `tqdm`
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
The loop also re-checks after `_run_city_channels` returns, which matters twice: the 60 s inter-city sleep would otherwise burn a full minute of the stop window (PEP 475 *resumes* `time.sleep` after the handler runs rather than returning early),
and on the **last** due city there is no next iteration at all, so the night would have summarized as complete while that city's remaining channels went uncollected.
**(3)** The child killed by our own stop is **not** recorded as the city's failure.
`KillMode` defaults to control-group, so the SIGTERM reaches the whole cgroup and the in-flight child returns `-15`
— a code in neither `HOST_BY_EXIT_CODE` nor `HOST_BY_BUSY_EXIT_CODE`, so it read as an ordinary collection failure.
This defect was invisible until (1) and (2) landed, because the SIGKILL destroyed the tail before the alert could be sent;
fixing them without it would have traded a silent failure for a **false alarm on every deliberate stop**
— `record_attempt(success=False)` burning one of five `consecutive_failures` that nothing but a success resets, and `attempted > succeeded` alerting (prod `failure_threshold = 1`) and exiting nonzero.
The check sits *after* orphan salvage (so anything the child finished is still cataloged) and *before* `attempted += 1` (so a channel we killed is not counted at all), mirroring how the blocked/busy branches `continue` before that same line.
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
A child that exits with a `HOST_EXIT_CODES` status trips the per-IP **host breaker** (see the host-lock section): that host's channels are skipped for the rest of the night, no city is marked failed, and the night alerts unconditionally and exits nonzero while still publishing.
Because makelab1 is shared, a `[resource_guard]` pre-flight (pure `plan_connection_limit`, Linux `/proc` read) lowers each run's `--connection-limit` when host load/free-RAM are tight — on top of the systemd unit's static CPU/RAM caps.

## Channel order, and the four rationales it did not have (issues #240, #238)

**The rule is "most expensive first, EXCEPT where truncation is cheapest to absorb."**
`SchedulerConfig.enabled_providers` returns a fixed rank — gsv, `gsv_streets`, mapillary, `mapillary_streets`, kartaview — and the docstring there states the rule; this section holds the mechanism and the history, because the docstring was the wrong size for it and because being the collision point for every branch that touched the ordering is how the wrong versions kept getting copied.

**The mechanism is the deadline clamp, and it is the only wall-clock lever ordering has.**
`remaining_s` is read fresh at every launch — one `time.monotonic()` per *launched* channel, in the launch pass — and `city_timeout_seconds` clamps the derived timeout down to it, floored at `_MIN_CLAMPED_TIMEOUT_S` (300 s).
A channel launched later therefore sees less of the batch deadline, and an expensive one launched late can have its timeout truncated to the floor and be SIGKILLed part-way, which costs its whole spend from the daily ledger (`db.add_api_usage` runs in the child, after the download returns).
So the channel needing the most wall-clock should start while the most of it remains.
`test_the_deadline_is_a_submit_gate_and_every_lane_child_gets_its_own_remaining_s` pins this, as a decreasing sequence in submit order.

**The rule inverts past one point, which is why kartaview ranks last rather than first.**
"Expensive first" holds only while no single channel is long enough to consume the deadline by itself.
One that *is* starves everything behind it — put it first and its siblings launch against what is left, down to the floor — so for that channel the question stops being which is most expensive and becomes which can best absorb being truncated.
A multi-hour KartaView sweep is that channel: last, exactly one channel eats the clamp, and it is the one #239 checkpoints, so a killed sweep resumes instead of re-paying for the cells it already fetched.

**Since #290 the order also decides who FETCHES and who REUSES.**
`mapillary` (rank 2) launches before `mapillary_streets` (rank 3), so within a city the grid run pays for the shared z14 census and the walk reads it for zero requests; `kartaview` (4) and `kartaview_streets` (5) are the same pair over the radius sweep, wired in #258.
Measured on the first KartaView walk (Krabi, 2026-08-31): 87 sweep requests un-paired, against the 18,851 that same walk costs on `gsv_streets` at one request per on-street sample — and 0 on any night the grid run got there first.
That is a consequence of the existing ranking rather than a new constraint on it — reversing the pair would simply move which channel's ledger carries the spend, and `census_fetched_by` would record that faithfully either way — but it is why the two are ranked adjacently and why nothing should separate them.
Nothing else here has that (Mapillary's checkpoint is #256, and a truncated tile census re-spends against a 3,500/day per-IP ceiling).
**Cheapest is not free, in two ways that both matter.**
No channel keeps its ledger row through a SIGKILL, whatever its provider.
And a SIGKILL still counts a `consecutive_failure` — only a *deliberate* pause (exit `SWEEP_INCOMPLETE_EXIT_CODE`) is amnestied — so the resumption that justifies this ranking is itself bounded at five nights.
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
  The batch deadline is checked in `_run_city_loop`, **between cities**; the launch pass has no deadline gate at all — only the lane cap, the SIGTERM submit gate, host affinity and the budget guards.
  Once a city starts, every one of its channels is attempted whatever the order, so truncation does not operate at channel granularity.
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
If the set ever widens, the fix is reserved slots per opt-in channel, not an unbounded hoist — filed as #282, because the mechanism's success case and its starvation case are the same case at different N.
Until it lands the only guard is an alert: `cmd_run_due` logs a `WARNING` when the hoisted count reaches `max_cities_per_day`, naming the channels that will therefore collect nothing, since the arithmetic that produces a night of zero `gsv` is otherwise recoverable only by subtracting two numbers on an INFO line.
That is the cost `docs/provider-access.md` records for `--provider` filtering, made permanent and universal; the expensive-city problem it would have solved is #239's, which does not require the trade.
**What lanes buy is lanes, not throughput.**
Each channel keeps its own limiter and its own daily budget, so no provider is asked for anything faster or larger than before; what stops is independent work queueing behind unrelated work.
**The safety argument is host affinity.**
The launch pass computes the set of per-IP hosts the in-flight siblings hold (from `CHANNEL_HOSTS`, never a hardcoded list) and *defers* — leaves pending, silently, reconsidered when a sibling completes — any channel that intersects it.
So each of Overpass, the Mapillary tile CDN and KartaView sees at most one talker from this process, exactly as before, and the configured `max_requests_per_minute` stays the real figure rather than doubling.
With today's six channels the effective ceiling is therefore **4**, whatever the knob says: `mapillary_streets` shares Overpass with `gsv_streets` and the tile CDN with `mapillary`, so it always runs after both — which is also the desirable order, since the second street channel of a city then hits the warm GraphML cache instead of racing for the same Overpass fetch.
`kartaview_streets` (#258) is the third Overpass channel and the second on `kartaview.org`, so the largest host-disjoint set is `gsv` (no per-IP host) + ONE of the three Overpass channels + `mapillary` + `kartaview` = 4 of 6.
The sixth channel is what this paragraph used to say re-deriving would cost, and it cost exactly that: the ceiling did not move, the denominator did.
The figure is a property of the channel SET's host graph, never a constant — derive it again for a seventh rather than quoting this number.
The child-side per-host lock (#208) is unchanged and still covers the manual runs the parent cannot see.
**Everything except the child itself runs on the main thread.**
A lane worker calls `_run_one_city` and nothing else; pricing, both budget gates, the ledger read, the resource guard, the breaker and *all* classification (busy/blocked/salvage/killed-by-stop/`record_attempt`) stay on the thread that owns the catalog, because `db.connect` opens it `check_same_thread=True`.
The two values `_run_one_city` would otherwise derive from `conn` or the clock are precomputed at the launch site and passed in (`timeout_s`, `estimated_requests`); the scheduler hands the worker `conn=None` deliberately.
That also keeps the read-then-write budget guard honest — the reads are serialized by being on one thread, in submit order, so two channels cannot both see "under budget" and both spend.
**At the default of 1 the channel body runs INLINE on the calling thread**, not on a size-1 pool: that is what makes the default byte-equivalent to the pre-#240 loop and what keeps every existing test's `_run_one_city` substitute able to touch the fixture connection.
**Neither budget gate applies to a channel `CHANNEL_RESUMABLE` marks (#274), which today is `kartaview` alone.**
Both gates exist because every other channel is all-or-nothing — a partial grid, tile census or road walk is not a run — so refusing to start is honest and `est > budget` is a real dead end.
A sweep checkpoints, so it is launched with `budget - used` as its cap whatever the estimate says, and its estimate is deliberately not consulted: `estimate_kartaview_requests` prices the whole sweep even for a resuming city, because its observed tier reads a `runs` row and a paused sweep never reaches `register_run`.
The single floor left is `_MIN_SWEEP_LAUNCH_REQUESTS` — a budget that runs out during radius *calibration* raises a plain `DownloadError` rather than `SweepIncompleteError`, so it takes no amnesty and counts a real failure, and the floor is derived from the calibration ladder's own bound so retuning the ladder carries it.
The property is declared as data, not as `provider == "kartaview"`: `CHANNEL_RESUMABLE` means "accepts a request cap that pauses and checkpoints rather than failing", which is why both Mapillary channels are `False` despite checkpointing their tile census (#256) — `download_mapillary` has a pacing knob and no request cap at all.

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
**`[download].connection_limit` is divided across lanes, not handed to each child whole.**
The resource guard reads host-wide pressure and only ever *lowers* its answer, but it is consulted once per child from a sample taken before that child's siblings have ramped — so at N lanes each child reads a quiet box and each takes the full limit, and the guard structurally cannot see the load it is about to permit.
Only three channels carry the number at all: the `gsv` grid, the `gsv` road walk, and the Mapillary road walk.
The Mapillary **grid** never receives it (`cli.py` omits the argument, so `fetch_city_images_async`'s own default of 5 applies), so the arithmetic is 50 + 50 + 5 at knob 3 rather than 150.
Combined with affinity, the only overlapping pair that points two full-size connectors at one third party is `gsv` + `gsv_streets` — both Google — which is 100 concurrent sockets on the same endpoints gate (2) below is already about.
Dividing makes the knob a no-op at 1 and bounded above it; the trade is that a city with a single enabled channel gets the divided share too, so **raise `connection_limit` deliberately when you raise the knob** rather than discovering the multiplication in production.

**Two things gated raising it in production. The first is now satisfied; the second is still outside this repo.**
(1) **Resume for every provider**, because a deadline or a `systemctl stop` now kills up to N children at once instead of 1.
This is **met as of #256**: GSV grid (`.downloading` sibling), the GSV road walk (same `collect_points_async` engine), KartaView (`checkpoints/`, #239) and both Mapillary channels (`checkpoints/`, #256) all resume,
so a killed child costs the tiles it had not yet fetched rather than the ones it had — which mattered here because a re-spend lands against the deliberate 3,500/day per-IP ceiling, i.e. ban risk under #241 rather than merely lost time.
A killed child still records no `api_usage` at all (#238), and that loss multiplies by N — unchanged by the checkpoint, since it is the parent that never sees the number.
(2) **`gsv` and `gsv_streets` hold no per-IP lock**, because Google meters per Cloud *project* rather than per IP — so running them together is only safe while `GMAPS_API_KEY` and `GMAPS_STREETS_API_KEY` really do live in **separate projects**.
Nothing in this repo records which project either key belongs to; it is a console check, and a shared project must be fixed by splitting the keys, never by inventing a fake `CHANNEL_HOSTS` entry (that would couple the night-level breaker to a condition that is not a host refusal).
**Rollout:** land at 1 everywhere and diff two nights' summary lines against history to check the byte-equivalence claim in production; then the two gates above; then flip prod to **2 before 3**, since `gsv` is the long pole and already overlaps each short channel in turn at 2, for half the blast radius of the unmeasured (cgroup memory sum, log interleaving).
Above 4 buys nothing with today's five channels.
Watch `MemoryPeak` after each night rather than pre-raising the unit's `MemoryHigh=20G`/`MemoryMax=24G` (sized for one worst-case child plus 30%; crossing `MemoryHigh` throttles *all* lanes into the documented reclaim stall), and if they must move, move High and Max together, keep the unit's quoted prose figures in step, and re-copy + `daemon-reload`.
`TimeoutStopSec=30min` needs no change: it prices the tail, which concurrency does not touch, and N children wind down in parallel.
The before/after is a measured question and therefore owes a writeup: `scripts/night_length_analyze.py` lands with the code and reads the elapsed distribution (with per-channel `api_usage` and the busy/blocked counts beside it, as the volume control) straight out of `logs/streetscape_scheduler.log*`; `docs/experiments/night-length.md` follows once there are nights on both sides of the flip to compare.
That is also why `cmd_run_due` logs `max_concurrent_channels=N` on its opening line — which setting a night ran under has to be recoverable from the night's own record, not from an operator's memory of the flip date.

## The subcommand roster, and the production config (added 2026-08-25)

Written 2026-08-25, when the CLAUDE.md rewrite turned its command cheatsheet into a table and two subcommands turned out to be documented nowhere.

**`assign`** (re)computes the `day_of_cycle` stagger for every enabled (city, provider) over `[schedule].cycle_days`, via `db.assign_schedule` — the rebalance handle after registering or enabling cities in bulk, so the nightly slate stays level rather than front-loaded.
It writes `day_of_cycle` and nothing else, which is what keeps the nightly `assign` (`run-due` calls it before every night) from un-enrolling every opted-in pair; a pinning test says so.
Assignment is **not** enrolment: on an opt-in channel it creates a row per enabled city and leaves `member` NULL, so the channel gains ~1,144 rows that collect nothing.
`status` and `assign` therefore print a per-channel enrolled count for each opt-in channel — without it, a table of blank `DUE` cells reads as "the flip did not take".

**`enroll-city CITY --channel CHANNEL [--remove | --clear] [--list]`** is the operator handle for `schedule_state.member` (#248), and it exists because hand-SQL has four ways to be a silent no-op here: `day_of_cycle` is `NOT NULL` with no default so a bare `INSERT` fails; an `UPDATE` matches zero rows and exits 0 whenever `assign` has not yet run with the channel enabled; a typo'd slug is the same zero-row success; and NULL/0/1 is three-valued with its meaning in a code-side table.
It refuses (`USAGE_EXIT_CODE`, changing no row) an unknown channel, a **default-membership** channel (per-city exclusion for `gsv` is `cities.enabled`, and a second less visible way to disable a city is how two operators disagree about why it stopped), an unresolvable city, and a city with `cities.enabled = 0`.
It deliberately does **not** refuse while the channel is still unwired or unconfigured — enrolment must precede the config block or the rollout order is impossible — and prints a `NOTE` saying nothing collects it yet.
`--remove` writes an explicit `0` and `--clear` restores NULL; the two are indistinguishable to dueness today and kept apart because only the explicit `0` survives a future flip of the channel default.
`--list` is scoped differently from the rest because it is read-only: it accepts a **default-membership** channel too (the answer there is every enabled city), and it refuses to run beside `--remove`/`--clear`, which argparse's mutually exclusive group does not cover and which would otherwise be accepted, ignored and exit 0.
A known, deliberate foreclosure: a **kartaview-only city is inexpressible**, since registering one makes it `enabled = 1` and therefore a member of all four default channels.
Revisit that only if widening wants Grab-market cities we would not otherwise collect.

**`notify-failure`** emails the recent scheduler-log tail and is wired as the unit's `OnFailure=` hook (`deploy/systemd/streetscape-tracker-notify@.service`), so a crash that never reaches the in-run alerting still produces an email.
It exits 0 when it alerted (or alerting is intentionally off) and 1 only when a send was attempted and failed, so the notify unit's own status is meaningful.
`run-due` returns nonzero on any failed city, so this hook can double-report a failure the in-run threshold alert already covered — accepted, since the alternative is a class of silent nights.

**Production reads `config/scheduler.makelab1.toml`, not `config/scheduler.toml`** (passed via `--config`; the filename is historical — the service itself runs on makelab2, guarded by `ConditionHost=makelab2*`).
The two diverge materially — budgets, absolute paths, `[publish].enabled`/`[publish].local` — so an operational change edited only into the repo default changes nothing in production, and vice versa: keep any comment-level rationale in step across both files.
