# Tests

What the test suite pins, and why each pin exists. Read before adding, moving or deleting a test,
and before changing behaviour that a listed test names.

Split out of `CLAUDE.md` (2026-08-22); the router keeps this topic's short rules and points here for the evidence and detail.
An edit that changes a rule belongs in both files; anything written since the split is under its own heading and says so.

`tests/`, pytest. No real network; the downloader tests substitute an in-memory fetch primitive rather than mocking HTTP.

## The CLAUDE.md router (issues #252 and #254)

`tests/test_claude_md_router.py`. The router split shipped two defects of its own, and the
convention it preserved shipped a third, so each of these corresponds to something that has
already gone wrong once:

- `CLAUDE.md` staying under Claude Code's 150,000-char limit — it reached 182,499 by growing about a paragraph per PR, which is structural rather than a one-off, so this fails while there is still room to act rather than after the file has silently truncated
- Every `docs/` link in the router resolving (it named `docs/experiments/README.md` for months before that file was written) and every split-out doc being named by the router, so nothing is left with no way in
- The router and `docs/experiments/README.md` naming the same writeups **and naming them in the same alphabetical order** — they shipped out of sync on the split's first commit, and the order is not cosmetic: appending guarantees an adjacent-add conflict where alphabetical insertion usually lands two branches at different offsets
- No doc writing a long option with an underscore, anchored so a slug like `saskatoon--sk_...` is not read as one; the split introduced an underscored spelling of `--network-type` into the file every session reads
- No prose line over 700 chars in any tracked `.md` file, fenced code and table rows exempt (issue #254) — above the measured 595-char maximum with room, and far below the paragraph-as-one-line shape it exists to refuse. The convention is not self-enforcing otherwise: the next paragraph appended as one long line reads fine, renders fine, and quietly restores the hotspot
- Every SHA in `.git-blame-ignore-revs` still being an ancestor of `HEAD`. A rebase or squash rewrites the commit a formatting entry names, and the stale entry then does nothing at all — `git blame` ignores an unknown rev in silence. This has already happened once, rebasing #254 onto main

## Naming, catalog, diffs and JSON

- Pure-logic tests for naming, db (incl. the v1→v2 migration against embedded v1 SQL), diff, JSON v2/aggregate v3
- `scripts/purge_tainted_runs.py`'s re-arm, in all three membership states: a default-membership pair (it really does re-arm, and says so), a non-member on an opt-in channel (the `UPDATE` succeeds and re-arms **nothing**, because membership gates before staleness — so it warns and names `enroll-city` rather than logging a re-arm that did not happen), and an enrolled pair on that same channel (back to normal, which is what keeps the warning a statement about the pair rather than the channel).
- The v12→v13 migration (`schedule_state.member`, issue #248): the column arrives NULL on an existing row and the row's other columns survive; re-connecting is idempotent; and — the claim that actually matters — a v12 catalog's due set is **element-for-element identical** afterwards, order included, since the city cap truncates from the tail and a test that only counted due cities could stay green while the gate changed which cities lead.
  The v8-to-current ladder test gains a v13 assertion for the reason it exists: a rung that never fires is silent.
- The v13→v14 migration (`census_fetched_by`/`census_fetched_at` on `runs` and `street_walks`, issue #290): the pair arrives NULL on existing rows, `register_run` round-trips it, and it defaults to NULL rather than to the collecting channel — provenance is recorded, never inferred.
  The load-bearing pin is `test_run_row_carries_every_runs_column`: `_row_to_run` builds `RunRow(**dict(row))` from a `SELECT *`, so a column without a matching dataclass field is a `TypeError` on every `get_latest_run` against a migrated catalog, not a missing feature.
- An end-to-end migration test with synthetic fixtures

## City registration manifests (issue #110, and the purposive additions)

`tests/test_register_frame.py` pins the bulk-registration safety rails — overlap reuse, `enabled = 0` until vetted, the GeoNames center guard against a province-centroid geocode, dry-run-by-default writing nothing, idempotent re-runs — with the geocoding seam monkeypatched, so no network.
It pins `--notes-label` by MUTATING it rather than by asserting the default: a case that only read `cities.notes` after a default run would stay green while the flag was dropped on the floor, and that label is how the vetting and enable steps select one batch out of the catalog.

`tests/test_mapillary_360_cities_manifest.py` IS the provenance of `mapillary_360_cities.csv`.
The city list is curated, but its values are a GeoNames join keyed by `geonameid` and there is no generator script to name in a `generated_by`, so the test re-runs that join in reverse: city, admin, country, continent, population and coordinates must match the vendored records, and `query_string` must equal what `build_worldwide_frame.query_string` writes, so a query here behaves exactly like a frame query at the geocoder.
The `city_id`s are pinned as **literals** deliberately — registration freezes them into filenames and published URLs, so a change in `db.derive_city_id` or `naming.sanitize_city_query_str` must break a test instead of quietly renaming a city — and the single deliberate departure from GeoNames' ASCII names (`Malmoe` -> `Malmo`) is a named constant rather than an unexplained mismatch.

## `--provider` is a channel list (issue #247)

`tests/test_cli_policy.py` pins the grid CLI's `--provider` flag, and (issue #290) the CLI seam of the census cache: the downloader is handed one `CensusCache` policy built by `crawl_store_for` — its path keyed on the PROVIDER (KartaView gets its own, gsv gets none), `reuse` False only under `--refetch-census` and untouched by `--force`, and the run's `run_date` riding along so a backdated snapshot refuses an entry observed after it; the `runs` row records who paid and when, a gsv run leaves both NULL, and a reused census says so in the printed summary.
The default expands to `gsv,mapillary` and **does not** include KartaView, while `--provider all` reaches the provider the default leaves out.
That asymmetry is the point: `all` derived from `naming.KNOWN_PROVIDERS` cannot silently OMIT a fourth provider, where a redefined `both` would have silently INCLUDED KartaView, adding a third mandatory credential and an hours-long serial sweep to every bare `streetscape_tracker.py "City"`.

- Typing nothing lands in `async_main` as the canonical parsed LIST, quietly.
  argparse runs `type` over a *string* default and hands a non-string default through untouched, so `default=DEFAULT_PROVIDERS_ARG` is load-bearing and invisible — and nothing pinned it until PR #263's review, because `run_cli` always passed `--provider`, leaving the whole suite green over a default the CLI never once parsed
- Every `KNOWN_PROVIDERS` member reaches **its own** downloader, asserted as set equality against a hand-kept map in the test.
  `--provider all` is the fourth consumer of that tuple and was the only one without a reachability pin — `PROVIDER_RUN_DTYPES`, `vis.PROVIDER_DISPLAY` and `scheduler.CHANNEL_HOSTS` each have one so a token cannot fail open.
  It mattered more here than at those three, because `all` expands from the tuple: a provider added to `KNOWN_PROVIDERS` but not wired reached the dispatch without anyone typing its name, and GSV was the `else` — a Google-keyed grid sweep published as the new provider's series, in an immutable dated snapshot.
  The companion test drives that branch (patching both `KNOWN_PROVIDERS` bindings) and asserts the log names `_collect_one_run`, so it cannot pass on `naming`'s own guard several steps earlier
- `both` is still accepted and warns on stderr (cron entries, shell history and `run_cities.py` pass-through arguments carry it), and resolves through the `DEFAULT_PROVIDERS` tuple rather than by re-splitting its argv spelling.
  That last part is a fix, not a flourish: the string form is also help text, so reformatting it to the more readable `"gsv, mapillary"` is an ordinary edit, and the unstripped split it used to do then resolved `both` to `['gsv']` and exited 0
- Duplicates collapse and the result is ordered by `KNOWN_PROVIDERS` rather than by what was typed, matching `scheduler._select_providers`
- An unknown name, an empty selection (`--provider ""`, `--provider ,` — both reachable, since argparse hands the type function whatever string it was given) and a street channel name all exit 2 with no downloader call
- `--provider all` fails fast on **every** named credential, collecting nothing when one is missing.
  That records a decision rather than an accident — the alternative, skipping a credential-less provider with a warning, was live and was refused, because a run that silently collected two of three while exiting 0 is the quiet-narrowing failure this codebase refuses everywhere else
- `--check-boundary` previews with **no** credential loaded at all, `--provider all` included.
  It contacts no provider API, only Nominatim; behind the fail-fast check it demanded three keys to draw a rectangle, on exactly the under-provisioned host an operator reaches for a preview from

One deliberate absence: the alias is pinned by its own named test, and the unrelated tests that used to pass `provider="both"` incidentally were moved to the list form.
Incidental coverage of a deprecated spelling trains readers to ignore the notice, and disappears the moment someone tidies an unrelated test.

## Mapillary tiles, and imagery-type stratification (issue #116)

- Mapillary tile math/decode/grid assignment (tiles built with `mapbox_vector_tile.encode`, end-to-end download served from memory)
- Imagery-type stratification (issue #116: decode keeps flats tagged, flat-only points become FLAT_ONLY rows with a null date, any-imagery vs 360° coverage, the `status_flat_only`/`num_flat_images` catalog columns + v6→v7 migration, and the JSON/aggregate any-imagery fields; plus frontend node tests for `METRICS.coverage_any` and `adaptCityRecord` normalization)

## GSV downloaders, and the history harvester

- GSV history harvester (response parsing, dated-only filter, cross-grid dedup, circuit breaker, resume — endpoint mocked)
- GSV batch downloader's quota-throttling behavior (OVER_QUERY_LIMIT retry, sub-threshold residual written back as a failure row, over-threshold abort
  — the `fetch_gsv_pano_metadata_async` primitive is monkeypatched to serve responses from memory)
- Tainted-run purge tool

## Scheduler

- Scheduler due/budget/provider-pairing/timeout-derivation logic
- The census cache at the scheduler seam (issue #290): `_channel_estimate` returns 0 on a probe hit and its full estimate otherwise, only the census channels read it (gsv and `gsv_streets` query per point and must not be priced off a Mapillary entry), each channel reads its own PROVIDER's entry,
  an expired entry prices at full cost, and — the one that keeps the two estimators honestly separate — **a cache hit must not collapse a child's timeout**, since `estimate_requests` also feeds `_mapillary_timeout_seconds`.
  Plus the dry run naming a cached census at ~0 requests, an entry that would expire during the batch priced as the fetch it will become (the probe's window is narrowed by `max_batch_hours`), the tail pruning expired entries and counting them, and a tail that survives a cache directory it cannot read.
- Per-city timeout derivation, one case per channel, all of them pure-function calls against a `SchedulerConfig` built inline (no mocking): that a huge GSV grid derives points ÷ achieved rate rather than the flat floor and a small city keeps the floor, that an Austin-sized grid clears the ~170-minute download that used to eat the whole 180-minute floor, that a paced tile census outgrows the floor at Anchorage's ~6,500 tiles and follows the *channel's own* rate, and that `0` means "pacing disabled" rather than "use the default" (so the floor stands).
  The point of every one of them is the same failure: a SIGKILLed child records **no** `api_usage`, so a timeout that fires mid-run loses the whole spend from the daily ledger and burns one of the five `consecutive_failures` that nothing but a success resets
- The KartaView cost arms (issue #238), which were written while **no runtime path reached them** — `UNWIRED_CHANNELS` refused the config block, so they drive `SchedulerConfig` directly. `config/scheduler.toml` declares the channel since #248 and the arms are live; the direct-construction style is kept because it makes each one a pure-function assertion rather than a night to run.
  They pin that the estimate is the *lattice* and not the GSV grid formula it used to fall through to (asserted as three orders of magnitude apart, in the direction that matters);
  that it carries the study's measured **1.80×** overhead and not the **1.54×** the same study reports against a different denominator;
  that a prior run's `runs.api_requests` *beats* the geometry, which is the only thing that sees a city calibrated to r=500 — pinned as the ~4× RATIO rather than a round ceiling, on a fixture sized to the study's real 2,547 km² Singapore, since the ratio is the finding and a fixture that is not the city it names cannot exhibit it;
  that the same prior *loses* to the geometry once the frozen grid is re-registered larger under it, which is the one direction that costs (a stale prior prices a bbox that no longer exists, and this is the only channel whose cost tracks bbox area directly);
  that a metro sweep outgrows the flat floor, pinned as a sandwich against its own paced wall-clock so the premise gets re-measured rather than the constant nudged;
  that the achieved-rate fraction is **below** the tile census's, which is the counter-intuitive direction and therefore the one worth pinning;
  that the channel sorts last because the rank table says so and not because `rank.get(p, 99)` happened to agree;
  and that the child is handed the configured pace, without which the derived timeout would be measured against a rate the sweep never used
- A checkpointed sweep pause (exit `SWEEP_INCOMPLETE_EXIT_CODE`) taking the same amnesty as a busy or blocked host, and — the case that gives the amnesty its point — a city still being **due** after eight consecutive paused nights, well past `max_consecutive_failures`.
  A multi-night sweep is normal operation for this channel rather than an edge case (Singapore is ~10.4 h of pacing against a 10 h `max_batch_hours`), so charging a pause would fire the failure counter before the sweep it protects could ever finish.
- The `UNWIRED_CHANNELS` cluster, which now drives a **synthetic** entry via `monkeypatch.setitem` on a real channel, because `kartaview` — the channel it was written for — became a real one in #248 and the dict is empty. Kept rather than deleted: the record/drop/don't-raise asymmetry is what the next unwired channel inherits. `test_every_unwired_channel_is_a_real_provider_token` **skips** on an empty dict rather than passing vacuously, since `set() - … == []` is true for the wrong reason and reads as "checked" in a green run
- The repo default's KartaView flip: that `config/scheduler.toml` declares the channel AND that it is opt-in, the second half being what makes the first safe — a default-membership kartaview would queue all 1,144 cities and nothing else in the config would say so. `scheduler.makelab1.toml` is asserted NOT to carry it: the production flip is a separate deploy decision, the same posture as `max_concurrent_channels`
- Per-(city, channel) membership and the opt-in hoist (issue #248), which are tested as two separate halves because either one alone is a channel that is configured and collects nothing.
  Membership: that `CHANNEL_DEFAULT_MEMBERSHIP` covers every scheduled channel by **set equality** (a `.get(p, True)` would classify a newly added provider as "every enabled city, immediately", which is how #225 phase 3b created this bug); that `get_due_cities`' `default_membership` has **no default value**, since the fail-open direction here costs a 186-hour nightly queue; that membership and the `consecutive_failures` quarantine are independent gates, so a non-member never reads as a failing city; and that `assign_schedule` — which `run-due` calls before every night — cannot un-enrol a member.
  The hoist: that it is the **identity permutation** with no opt-in channel configured, asserted element-wise against the pre-change union order rather than eyeballed; that a city due *only* on an opt-in channel leads the slate; that a city due on `gsv` too keeps its **exact** union position (the `all` key, not `any`); and that a paused sweep therefore leads the next night's slate, which is the claim `docs/scheduler.md` makes and the mechanism the "five consecutive nights" budget depends on.
  Both unfinished-sweep arms are pinned, because only one of them is live: the deliberate pause consumes no city-cap slot, while the **timeout SIGKILL** — the arm a nightly batch actually takes, since `_run_one_city` passes no `--kartaview-max-requests` (#273) — counts a `consecutive_failure` and *does* consume a slot, yet still leads the next slate.
  Testing only the pause would leave the live path uncovered and its opposite cap behaviour asserted as if it were the rule.
  The count itself is pinned in both trivial directions: an all-opt-in slate (`--provider kartaview`) reports **no** hoist, because an all-0 key set is the identity permutation just as an all-1 one is, and reporting it would name the whole slate as reordered on every catch-up.
  And since the hoist is deliberately unbounded (#282), the night's `WARNING` when the hoisted count fills `max_cities_per_day` is pinned against a slate where a `gsv`-due city really is left uncollected — the alert is the only guard until reserved slots land, so it must not be merely pessimistic.
  `enroll-city`'s refusals are pinned as `USAGE_EXIT_CODE` with **no row written** — an unknown channel, a default-membership channel, an unresolvable city, a disabled city — alongside the two it deliberately does not refuse: an unwired or unconfigured channel, since enrolment has to precede the config block or the rollout order is impossible.
  `--list` is pinned as scoped the other way on both axes, because it is read-only: it refuses `--remove`/`--clear` (which argparse's mutually exclusive group does not cover, so they would otherwise be accepted, ignored and exit 0 — the silent no-op the command exists to prevent), and it *accepts* a default-membership channel, where the answer is every enabled city.
  The enrolment cost note is pinned to say when it is the **geometry** tier, since no city has a cataloged KartaView run and that tier is ~4x low on an r=500 metro, with nothing downstream to absorb the miss (#273).
  Paired with the boundary case, which pins the amnesty as narrow: a sweep SIGKILLED by its timeout **does** still count a failure, because a kill has no exit code and nothing can distinguish one that checkpointed real progress from one that made none
- Scheduler passing each street channel its FULL daily budget (the collector subtracts today's spend itself, so passing the remainder double-counts it and fails cities that fit)
- `regenerate-aggregate` exiting nonzero when the driving-plan rebuild failed while still publishing the two artifacts that succeeded (the guard exists so #167's posture holds, but rebuilding the published JSON *is* that command's job, so a partial rebuild must not read as success to a wrapper)
- On-demand catch-up path (issue #214: the `--provider` filter running only the named channel and leaving the others' `schedule_state` untouched, the comma form meaning the repeated form and keeping gsv-first order, `assign_schedule` still covering the full enabled set, and refusal
  — with `USAGE_EXIT_CODE` and **without opening the catalog**
  — of an unknown channel, a disabled one, a value naming no channel at all, and a `--limit < 1`; that the usage code is distinct from 0/1/2 and from both host-code families; that `_select_providers` *raises* rather than returning a None that `_collect_due` would read as "every channel",
  with a signature assertion that no fail-open default exists; that `--limit` overrides `max_cities_per_day` **and reaches N cities even when candidates are skipped**
  — the regression from pre-slicing the candidate list, which collected 1 of 3 when two over-budget cities led the queue; that a widened run is still stopped by the daily budget ledger,
  which needs the stub to write `api_usage` as the real pipeline does; that a capped run does not sleep after its last city; that a filtered run **desyncs that city's paired snapshots**,
  asserted as the intended cost rather than left to be discovered; and that the summary carries both the active filter and the night's elapsed hours, since that line is the `[alerts]` email's content and the only place time-under-load is recorded)

## Concurrent channel lanes (issue #240)

**Added after the split.**
Fifteen tests cover the lane scheduler, fourteen in `test_scheduler.py` and one in `test_assess_city.py`.
They share `_stub_lane_collection`, whose fake records **both** edges of each collection — `("start" | "end", city_id, provider, seq)` under a lock — because everything here is an ordering claim ("did these two overlap?", "was that one ever started?"), and a `seq` taken under the lock beats a timestamp for exactly that.
It **never touches `conn`**, on purpose: the scheduler hands a worker `conn=None` because `db.connect` is `check_same_thread=True`, so a fake that reached for the fixture handle would go green over a design that cannot work off-thread.
Every wait carries a 10 s timeout, so a scheduler that has stopped overlapping fails red rather than hanging the suite.
**Two of these tests would pass against a scheduler with no host affinity at all if written the obvious way, and that shaped them.**
A deferred channel's absence is only observable by waiting, and the fourth channel — if it were wrongly launched — is a thread that does nothing, so it can still be *scheduled* after its siblings return; timing alone proves nothing.
So `test_channels_sharing_a_host_never_overlap_even_at_knob_4` holds the three disjoint channels open on a barrier and watches for the fourth for `_AFFINITY_PROBE_S` (0.5 s, elapsed in full only when the answer is right), and reads **submit order from the parent** (`city_timeout_seconds`, called once per launched channel on the launching thread) rather than from the workers' own start events, which race.
Verified by mutation: deleting the affinity gate fails that test and `test_a_mapillary_block_at_knob_3_…` on every run (8/8); forcing one lane fails seven.
`test_a_mapillary_block_at_knob_3_…` earned that "every run" the hard way — as first written it detected the deleted gate only 4 times in 6, because at knob 3 the first launch pass fills every lane and `mapillary_streets` is held back by the LANE CAP, which a scheduler with no affinity respects too.
The window that actually separates them is the launch pass triggered when a lane frees while `mapillary` is still in flight and still unclassified, so the test now stages exactly that: `gsv` is let go first, the other two are held until `record_attempt` shows `gsv` fully classified, and the fourth channel is watched for across the probe window.
The rule it teaches: a lane test that merely *observes* the right outcome has proven nothing until the mutation says otherwise, and at knob N the lane cap will happily impersonate the gate you meant to test.
What the others pin: genuine overlap at knob 2 (a barrier a sequential implementation cannot clear); the knob as a **cap** — three pairwise host-disjoint channels at knob 2 still peak at two; the breaker's read-then-act ordering surviving a *deferral* (`mapillary_streets` is never submitted after `mapillary` reports a block — the knob > 1 re-verification of the sequential breaker tests); busy-vs-blocked asymmetry at knob 3, since typing a busy local lock as a breaker would cost a whole night's Mapillary work over one manual run;
**budget skips being final while host deferrals relaunch** (a skipped channel is priced exactly once — re-pricing it every pass is how a deferral turns into a spin); a stop as a **submit gate** that lets in-flight lanes drain and credits them, then names the never-launched channels; the killed-child amnesty applied **per result** now that one stop kills N children; a worker exception draining and crediting its siblings before it propagates;
**and its mirror** — an exception on the MAIN thread, in classification itself, doing the same (`test_a_MAIN_thread_error_also_drains_the_siblings_and_still_trips_the_breaker`).
`record_attempt` hitting a locked catalog, or `logger.error` hitting a dead pipe, used to skip the rest of the drain, wait out the siblings in `pool.shutdown`, and then discard their already-paid-for outcomes.
The assertion there is `blocked_hosts` rather than a counter, because a host refusal lost that way means the night-level breaker never trips and every later city keeps firing at a host that already refused this IP;
that **every** lane exception reaches the log even though only one can be re-raised (`test_every_lane_exception_is_logged_…`), the `[alerts]` email carrying only that log's tail;
per-child `remaining_s` priced against the shared deadline at each channel's own submit; every pricing and ledger read landing on the main thread in submit order; a city's lanes draining before the next city is priced (paired snapshots + ledger ordering); knob 1 running **inline on the calling thread** in canonical order with nothing overlapping; the config parser falling back to 1 with a warning for `0`, `-2`, `1.5`, `"three"` and `true` (TOML booleans are Python ints, so a bare `isinstance` check would accept `true` as one lane);
and, in `test_assess_city.py`, that the operator path inherits the same lane scheduler — pinned there because `ASSESS_CHANNELS` is the shape that would hide a missing affinity rule by luck, since a canonical-order sequential run happens to get it right for the wrong reason.
**Deliberately not written:** "our own lanes never produce a 79/80 busy-skip."
It is vacuous at this seam — busy exits come only from real children hitting the real cross-process lock, never from a fake — so it is carried structurally by the affinity and drain tests and operationally by the rollout watch list in `scheduler.md`.
**One existing test was redesigned rather than repaired.**
`test_batch_deadline_stops_the_loop_but_still_publishes` drove `time.monotonic` from an iterator that stepped on every **read**, which made the batch's elapsed time a function of how often the scheduler happens to call the clock — not a contract, and #240 moved those reads.
It now uses `_WorkClock`, which advances inside the fake collection, tying the clock to time spent collecting.
The rule that replaces the old comment: stub the clock only with a work-advanced one, never with anything that steps on a read, and never in a knob > 1 test unless a single thread owns every advance (the deadline-pricing test qualifies — its advances happen in the launch path, which is main-thread by design).
**`test_night_length.py`** pins the measurement rather than the scheduler: nine tests over `scripts/night_length_analyze.py`, and every one of them is about *attribution* rather than parsing, since a night credited to the wrong knob or an incomparable night pooled into the comparison produces a speed-up number that is wrong quietly and in the flattering direction — a night with no start line reads as knob `null` rather than as sequential (defaulting it would invent a large favourable control group out of the whole pre-#240 corpus), and the `--provider` catch-up and the deadline-truncated night
— the two shortest in the fixture, for reasons that have nothing to do with lanes — are counted and excluded rather than dropped.
Three of the nine guard the negative controls specifically, and both hazards they cover are false readings of a **stop-the-line** signal rather than parse errors: two `run-due` invocations on one date are two observations of elapsed hours but only ONE day of `api_usage` (that table is keyed by (date, provider) and already holds the whole day), so counting the row twice inflates the lane side of the exact comparison the script exists to make;
and `hosts_unavailable` is anchored to the blocked-host note's own `; `-delimited segment rather than searched for anywhere in the summary tail, because that tail also carries the stop reason and the driving-plan error — a plan fetch failing `503 Service Unavailable` would otherwise report a per-IP refusal that never happened.

## Street coverage and road walks

- OSM street coverage (edge matching with hand-built geometries, frozen-network catalog registration — osmnx load mocked)
- Road-walk collector (issue #99: deterministic on-street sampling, fractional per-edge coverage with the distance/© Google guards, and the full `collect` CLI end-to-end
  — OSM fetch + the shared GSV request engine both served from memory, asserting artifacts + `street_walks` catalog row + isolated `gsv_streets` ledger + quota-retry reaching the streets path)
- Streetwalk manifest (issue #155: latest-per-(city, provider) collapse, null-stat tolerance, same-day re-collection, plus the scheduler/collect hooks that regenerate it)
- Mapillary road-walk arm (tile-census join onto sample points, the #116 status vocabulary at sample level, cost independent of spacing, and the chunked-vs-single-shot join equivalence)
- **Both channels walking one city on one run date without colliding** (the regression that the provider token exists for) and **both network types walking one city on one run date without colliding** (the same regression, for the network token
  — plus the v8→v9 rebuild preserving rows, the broad walk's footway/alley buckets, and `graph_to_edges` retaining `service`)
- **NO_DATE counts as coverage** (issue #257, `tests/test_streetwalk_coverage.py`) — three unit tests, each pinning one half of the definition rather than the arithmetic, and all three failing against the pre-fix `ok = m["status"] == "OK"`:
  `test_no_date_sample_counts_toward_coverage` (undated samples raise `covered_samples`, `coverage_fraction`, both `_any` variants and, through `summarize_streetwalk_coverage`, `length_km_covered` and both `coverage_pct_by_length[_any]`, while `total_samples` stays put — the change is numerator-only);
  `test_no_date_sample_contributes_coverage_but_no_age` (coverage triples while `nearest_pano_date` and `median_covered_age_years` do not move, and `dated_covered_samples`/`dated_pct_of_covered` record the denominator that makes the two legible apart);
  and `test_gsv_no_date_still_gated_by_official_copyright` (the same all-`NO_DATE` run under `© Someone Else` vs `© Google`, two halves so the exact-copyright gate is pinned as the *only* difference — a one-sided version would still pass if the fix had widened GSV to third-party imagery).
- **A walk on a paired night costs nothing** (issue #290, extending `tests/test_streetwalk_mapillary.py`): the walk reaches the provider-keyed cache entry the grid run writes while keeping its own channel- and network-keyed checkpoint (both mistakes are silent, so the two path builders are asserted side by side);
  a reused census writes `street_walks.api_requests = 0` with `census_fetched_by = "mapillary"` and charges neither ledger; its rows carry the cache's `crawl_started_at` as `query_timestamp` while a fresh census keeps this process's clock;
  `--estimate` and the `--daily-budget` pre-flight both price a probe hit at 0, which is what keeps a nearly-spent budget from refusing the cheapest walk of the night; and `--force` reuses while `--refetch-census` does not.
  Their companion on the grid-attribution side is `test_select_pano_points_counts_a_no_date_pano_as_present` in `tests/test_street_coverage.py` (611bd53); the two files pin one definition on the two halves of `streetscape_street_analyzer`, so a change to either belongs in both.
  Two things about the age test are deliberate and easy to undo by accident.
  **Its undated samples are the MAJORITY (10 of 15) and its dated ones carry dates a year apart**, because the obvious fixture — a few undated samples among many dated ones sharing one date — cannot detect the bug it exists to catch: fold the undated samples in at age 0 and the median does not budge, since the dated majority outvotes them.
  It also asserts against an **independently computed expected median**, not only against a `before` frame, for the same reason.
  And **the GSV-only unit tests structurally cannot reach the real case**: they call `compute_streetwalk_coverage` directly with `provider="gsv"`, the one provider whose NO_DATE population is empty in practice, so the end-to-end pin lives in `test_unusable_timestamp_becomes_no_date_not_a_bogus_year` in `tests/test_streetwalk_mapillary.py` — an all-undated census that reads 0.0% of street-km before the fix and 100.0% after, asserted on the artifact *and* the catalog row, since `streets.html` and the walk diffs read the row rather than the GeoJSON.
  The fixture helper `_collected` takes a `no_date_pred` that wins over `covered_pred` (a located pano with a null `capture_date` — the status and the date have to disagree in exactly one direction) and a `date` that may be a callable, so each covered sample can carry its own.
- Streetwalk-name repair script
- Walk-to-walk diffs (issue #101: gained/lost transitions vs. bare fraction shifts, overlapping counters yielding one detail row, intersection-only diffing of a refreshed network, series isolation across provider/network_type, the spacing/match-dist skip gates, "diffed, no changes" recording a row but no detail file, the v10→v11 migration, and the `coverage_by_highway` backfill script)
- v12 street-length columns (the v11→v12 migration preserving rows and **resuming after a partial migration**
  — the columns are added one ALTER at a time, so a guard checking only the first would strand the rest; the collector and salvage paths persisting the lengths,
  salvage tolerating a pre-v12 artifact rather than raising and forcing a full-cost re-crawl; the manifest publishing the lengths,
  trimming and order-preserving the per-class block, and surviving an unparseable one; and the backfill script's dry run, idempotency, rounding tolerance, wrong-artifact refusal, and the null-tolerant cases that must not become permanent candidates)

## The census seam

- Census grid-run tail (the three populations written in pano → FLAT_ONLY → empty order with the provider's own column order, a flat-only point's null capture date, a pano at a point outranking a flat there, the *first* flat at a point staying its representative
  — `np.unique` returns first occurrences sorted by value, so dropping the re-sort silently changes which image represents a point
  — an unmeasured area's points becoming REQUEST_FAILED rather than empty, the date binding receiving the whole census plus positions rather than a taken frame, and the ownership contract from both sides: the census gone from the fetch dict afterwards, and the caller's source asserted free of a local binding for it)
- Provider-agnostic census seam (`tests/test_census.py`, issue #225: the generic layer driven by a non-Mapillary schema, the two-rule dedup asserted at the layer it now lives in **including that `drop_duplicates(keep="last")` gets it wrong**, a null census id not overwriting the last real image, and the three ways an `image_columns` binding can silently publish a wrong column — omitted, misspelled, or colliding with the shared core — each refused rather than shipped)
- KartaView grid-run wrapper (`tests/test_kartaview_grid_run.py`, #225 phase 3b
  — the join between the sweep and the shared tail, which neither of those files can see: that the bindings actually reached are KartaView's,
  so the CSV carries its schema in its own order and its `shot_date >= date_added` rule survives to `capture_date` (asserted on two rows whose dates are BOTH plausible and non-null, since that is the case a null-check misses); that `failed_cells` land as REQUEST_FAILED rather than ZERO_RESULTS,
  and that `_points_in_cells` masks the **square** and not the 1.57×-larger circumscribed circle, tolerates mixed cell sizes and unwraps the antimeridian; that `api_requests` and `api_requests_total` reach different sinks; that the checkpoint is discarded **only after** the artifact is on disk,
  not at all without one, and never on a `SweepIncompleteError`, which propagates unwrapped and writes nothing; and that the checkpoint path is date-free, channel-keyed, moves when a frozen grid is resized, resolves a symlinked override and is never under `data/`)
- Columnar census (issue #157: the **golden run CSV** — `tests/fixtures/mapillary_golden_run.csv`, generated from the row-wise implementation and asserted byte-for-byte except the geodesic grid columns, since a formatting or ordering drift would fake a diff in every Mapillary city; a companion test guarding the fixture's own coverage, because a silently-narrowed fixture still passes the byte comparison while protecting nothing; a second companion guarding the *comparison*, since a grid tolerance wide enough to swallow a real regression leaves a golden test that passes no matter what ships
  — it pins acceptance of the measured cross-platform ULP noise against rejection of a float32-scale coordinate shift, reordered rows, a dropped row, a moved image coordinate, a changed status, and a differently-rendered null; and the scalar-vs-vectorized capture-date equivalence including the values pandas can represent and `datetime` cannot)

- Mapillary tile-census resume (`tests/test_mapillary_resume.py`, issue #256
  — the headline pin is BYTE IDENTITY: a census interrupted after one tile and resumed reproduces the **same golden fixture** the uninterrupted path is pinned to,
  which is what the tile-keyed part naming and the `tiles_for_bbox` reassembly exist for, since a reordering would read as imagery churn in every Mapillary city;
  a companion pins the border duplicate directly, serving a different payload per tile so the census records which copy won.
  Also: only successful tiles commit and a failed one is refetched while the tolerance denominator stays the full tile set; an empty tile gets a record and no part file;
  the parquet parts round-trip the census's extension dtypes, which is why they are not CSV (`"NA"`/`"None"` as provider-supplied strings, null `on_foot`, null `captured_at_ms`);
  each way a checkpoint can be unusable — format, bbox, zoom, channel, tile count, age, corrupt state, a part short of its recorded row count — discards and refetches **without raising**, and a six-day-old one still resumes;
  a failing commit warns exactly once and never fails the city, and an unwritable directory fetches unprotected; a city blocked before committing leaves no empty directory;
  the walk and the grid run get different checkpoint directories and a cross-channel resume is refused, as do the two `--network-type` walks of one city, which the channel alone cannot separate;
  and the two counters split, **including that a blocked night's refused requests reach the crawl total** — they are counted into `api_usage` deliberately, so without `_commit_spend` the resumed row would price the city below what it cost.
  Four pins come from the #256 review, and each fails loudly on the code as first written: the age cap is measured from the FIRST commit, so a night that commits no tile moves `updated_at` and leaves `created_at` alone — otherwise a nightly-refused city refreshes its own clock forever and the cap protects nothing;
  a run whose stale checkpoint was DISCARDED is still protected afterwards, since the discard deletes the directory the fresh handle is about to commit into;
  a part is fsynced before it is renamed, checked by recording the `fsync`/`replace` order rather than by trusting the call site;
  and a part written before any commit record is swept, which the loader returns too early to reach)

- The shared census cache (`tests/test_census_cache.py`, issue #290 — the plumbing both providers stand on).
  The headline pins are the ones a plausible edit breaks silently: the path keys the PROVIDER, city and bbox and carries no channel, variant or date (a channel-keyed entry would reuse nothing, with nothing failing), the checkpoint and the cache spell one bbox the same way,
  and the marker is INSIDE the directory before it is renamed — asserted at the `os.replace` call itself — so a failed marker write or a failed rename leaves the checkpoint exactly as it was, with no stray marker in it, and nothing under the cache name.
  Also: EXDEV falls back to a staging copy renamed into place, since either directory can be pointed at another filesystem by its env override; a stale entry is replaced wholesale rather than merged;
  the window is aged from `crawl_started_at` (a fresh promotion of a ten-day-old crawl is refused, which ageing from `completed_at` would allow; a marker without one is refused, since the builder requires it), last night's entry still reuses, and an entry whose crawl finished after the consumer's `run_date` is refused WITHOUT being deleted;
  the shared loader skeleton (`load_cached_store`) deletes on the provider's verdict and on a torn store but keeps an entry a `CacheEntryUnusableHere` says is merely not for this caller; the reuse accounting prices `api_requests_total` only for the (channel, variant) that paid, the variant included; the observation timestamp is the crawl's only for a reuse;
  a hit is reconciled with the consumer's own checkpoint — an older one is discarded, a newer one wins, the crawl's own entry with failed work is handed back marker-less for the resume to re-probe, and another channel's holes are inherited;
  the probe opens no parquet footer, refuses a marker whose recorded store format this build cannot read, and deletes nothing; the prune removes expired entries and day-old debris (`<entry>.tmp`, marker-less) while leaving fresh ones, a copy still in progress, stray files and the whole of `checkpoints/` alone;
  and `crawl_store_for`/`CENSUS_PROVIDERS`/`STORE_FORMAT_VERSIONS` are pinned as the one derivation the grid CLI, the walk collector and the scheduler all read.
- Mapillary census reuse (extending `tests/test_mapillary_resume.py`): a second channel reads the first's census for **0** tile requests and the reused census writes the **same golden fixture** the uninterrupted and resumed paths are pinned to;
  the counters split — `api_requests` always 0, `api_requests_total` the crawl total for a same-(channel, variant) re-finalize and 0 for a cross-channel reuse;
  the two `--network-type` walks share one census while keeping separate checkpoints; a cross-channel reuse inherits the failed tiles rather than re-probing them, while the crawl's OWN channel coming back re-asks exactly the hole and re-promotes an entry without it;
  a hit discards this channel's older partial checkpoint, an interrupted `--refetch-census` is resumed rather than abandoned for the entry it was started to replace, and a backdated `run_date` refetches rather than publishing rows from its own future;
  an interrupted or `degraded` crawl is never promoted, an incomplete or geometrically-wrong entry is refused (and the refetch that follows replaces it), a stale one is refetched;
  `--refetch-census` ignores the entry and leaves the fresher one behind; no `cache_path` is byte-for-byte the pre-#290 behaviour; and an unwritable cache never fails a city.
- KartaView sweep reuse and the first-commit stamp (extending `tests/test_kartaview_collector.py`, issues #290 and #272):
  the same reuse set as Mapillary's, plus that a hit restores `cells`/`cells_visited`/`raw_photo_count`/`radius_m` from the record (they describe the census, not the process), that a cross-channel reuse inherits the failed cells while the sweep's own channel re-probes them, and that an explicit radius or page size that contradicts the entry refuses it for THIS caller without deleting it for the others;
  the commit's durability pin now reads the part rename in `_commit_checkpoint` and the state rename in the shared `_write_json_durable`, since the state record goes through the one writer both providers and the cache marker share;
  that a `SweepIncompleteError` pause never promotes; and **that a failed `finally` commit never promotes** — the KartaView-specific trap, set up so the last periodic commit HAS landed, which isolates the `final_commit_ok` clause from the other three.
  For #272: `created_at` is stamped once and carried forward while `updated_at` moves, a night that sweeps nothing cannot restamp it, and a v1 record is discarded rather than read forward.

## Catalog backups (issue #145)

**The restore drill** — a populated catalog plus its `-wal`/`-shm` sidecars genuinely deleted, restored, and asserted intact/complete down to frozen geometry and schema version;
the same drill with the sidecars **left in place**, asserting the restore refuses rather than silently resurrecting the pre-restore rows, then yields the backup's own contents once they're moved aside;
verify-then-promote preserving the previous good backup when `integrity_check` fails, via a `_verify` seam since `conn.backup()` demands a real `sqlite3.Connection`;
the never-prune-the-newest retention guard and its corollary, a stale-but-successful backup reported unhealthy;
row counts matching the copy rather than the source;
sidecars cleared at promotion and prune;
a busy source timing out instead of hanging (a rollback-journal DB with a competing `BEGIN EXCLUSIVE`, the one arrangement where a reader genuinely blocks) and an open source transaction failing fast;
status written on failure too;
the pre-flight hook ordering **before** the city loop and on zero-due nights;
the `restore-backup` subcommand restoring and then refusing;
a backup failure alerting below the failure threshold + exiting nonzero while still publishing;
and the **per-writer staging name** — that two pids derive different paths, that a concurrent writer's in-flight staging file is neither unlinked nor promoted (its path derived *through* `_staging_path` under a faked pid, so reverting to a shared name makes the test fail rather than pass on a name it invented), and that abandoned staging files are swept by age while a live one survives
— plus an autouse stub, because `backup_dir` defaults into the working tree and the tail's pre-#145 backup had been dropping fixture-sized files into the repo's `logs/` for as long as it existed.

## Per-IP hardening: Mapillary, Overpass and the host lock

- Mapillary fail-fast (issue #205: a blocked host and a rejected token each stop within `connection_limit` requests instead of paying for all 361 tiles, `api_requests` equals what was actually issued, a host block is typed while a bad token is not, an HTML error page is also a block, the two #168 guards
  — a per-tile 404 still fans out to **every** tile and a tolerated failure doesn't truncate the city
  — and the fail-open guard, which simulates a future edit that swallows the fatal error and asserts the run still refuses rather than publishing an empty census)
- Overpass hardening (issue #209: the timeout we set is the one osmnx reads and the server-side `[timeout:180]` tracks it, only transport faults retry and a permanent error is attempted exactly once, a refusal is a `HostBlockedError` that no longer reads as `RetryError` while an empty bbox stays a **city** failure, the `/status` probe short-circuits a refusal but never fails a healthy fetch
  — a 5xx, a 406, a queued slot of any length and an osmnx internal going away all mean *proceed*, since a false positive there skips every city of every night
  — and sends our UA rather than the 406-triggering `python-requests` default, `_deadline` interrupts a hang, restores the prior signal handler, and is typed so a stray `socket.timeout` is not reported as a 429/504 hang, and the `OVERPASS_URL` mirror takes effect without a restart)
- Cross-process host lock (issue #208: a second holder fails fast with **zero** requests reaching either provider, the lock releases on a raising body, the two hosts don't contend with each other — a Mapillary road walk needs both
  — a **cached** network never contends, the lock dir resolves a symlinked checkout **and a symlinked `STREETSCAPE_LOCK_DIR` override, before that directory exists**, and is never under `data/`, and the scheduler source is asserted free of `host_lock` since a parent holding the flock would deadlock every child; the child half of the exit-code contract, which is where the breaker's input is actually produced and would otherwise be pinned only on the scheduler side
  — `cli.py` and `collect.py` returning the blocked code, the *busy* code, and a plain 1 for a mixed failure where only some channels hit the host, plus `_run_collection_subprocess` copying `returncode` onto the outcome and leaving it None on a timeout; and the breaker's loop behavior
  — one refusal skips that host's channels, only channels that need it, **no** `record_attempt` on a skip, five consecutive blocked nights leaving the city still returned by `get_due_cities`,
  a blocked night alerting below the failure threshold while still publishing, the same for a busy night but with a subject that does **not** claim the provider refused us,
  a busy exit leaving the *next* city's same channel still attempted, and `CHANNEL_HOSTS` covering every scheduled channel since it is read with a fail-open `.get`)

- Request jitter (issue #292: the spaced pacer sleeps exactly the **shifted exponential** of the injected draw — floor `(1 − j) × mean` plus `j × mean × draw` — keeps the configured **mean** rate and reaches `CV = jitter` over 20,000 real draws, has no burst credit after an idle, exceeds the old uniform draw's hard ceiling (the property the change exists for), `jitter = 0` never draws randomness and sleeps exactly as the bucket did, disabled pacing ignores it, and `[1, ∞)` or a negative is refused by the class, the shared `jitter_fraction` argparse type and `coerce_jitter` alike;
  the census builds its limiter with **the jitter and rate it was given**, not merely with the defaults — the mutation that hardcodes the module default at the construction site must fail, since `--mapillary-jitter 0` is this experiment's control arm;
  the flag reaches the downloader from `cli.py` and the census from `collect.py`, both defaulting on rather than to the metronome, and a jitter of 1 exits 2 at parse time;
  the scheduler parses `[providers.*].jitter` as None-when-absent — never 0, which means "restore the exact cadence" — **warns and falls back to None on an out-of-range or non-numeric field** rather than letting it reach a child as an exit-2 argparse error on every city every night, and hands a valid one to both Mapillary children, a 0 included, while an unset one and a GSV child get no flag;
  and the prod config pin asserts both Mapillary channels resumed at the same jittered rate)

## The same-day partner path (issue #215)

`tests/test_assess_city.py`: the channel set collected in `enabled_providers()` order and **never** including the GSV grid run;
refusal — with `USAGE_EXIT_CODE` and **without opening the catalog**
— of every unpaired `--width/--height`/`--lat/--lng` combination, of `--provider gsv` with a message naming `run-due`, of an unknown/disabled/empty channel, and of a config where no assess channel is enabled;
a non-TTY stdin without `--yes` refusing rather than hanging on `input()`, *after* the free pre-flight has still registered the city;
`--estimate` registering but leaving `api_usage` untouched;
registration capping at the 40 km ceiling and aliasing the query slug so a second run never re-geocodes;
`rect_in_boundary_frac` measuring the **rectangle** rather than the city
— 1.0 inside, ≈0.5 half-overlapping, 0.0 disjoint, and None for the Point that Nominatim returns for many places, which must never render as 0%
— plus the low-fraction warning naming the NKY precedent and a **raising geocoder not failing the run**, exercised through the real probe rather than a stub that returns None;
success starting only the collected channels' clocks while `gsv` stays absent so the nightly batch still does the grid run, and a failure recording **no** `consecutive_failures`, with six failed runs leaving the city still returned by `get_due_cities`;
a Mapillary tile block leaving the GSV walk collected while `mapillary_streets` is never asked, an Overpass refusal skipping both walks but not the grid census, and a busy exit skipping one channel only;
the tail regenerating before publishing, not publishing when nothing succeeded, and publishing a partial success anyway;
the answer report leading with street-km ahead of grid coverage and carrying the "NOT the deployment number" label, printing the any-imagery split for Mapillary but not for GSV, reading "not walked" rather than 0% for a missing walk, surviving NULL lengths/ages, and building the city-page link from the grid run's CSV filename
— preferring the Mapillary run, **falling back to the GSV one** and naming which it opened, saying there is no page only when neither exists, and tolerating a `site_url` with no trailing slash, since link building is bare concatenation;
that the closing note reports which channels are **actually** due, asserted against `get_due_cities` rather than against its own wording (the natural sentence, "due on every channel", is the opposite of true for the three it just collected) and staying silent about un-paired snapshots when nothing was collected;
that the pre-flight distinguishes "over the whole daily budget" from "over what is left today" — only the second is a deferral — and reports every Mapillary pacing rate in play rather than the last channel's;
that a config with publishing disabled prints the notice beside the link and still exits 0, that `--no-publish` does *not* print it (the operator chose that), and that no `--publish` flag exists at all
— pinned so the asymmetry reads as a decision rather than an omission;
and `_publish` passing `--local` iff `[publish].local`, read from the TOML and asserted true in the checked-in prod config.

## Google's driving plan (issue #176)

- Driving-plan archive (issue #176: dirty-date survival, hash dedupe leaving a snapshot row but no artifact/entries, the Yes→No `publish` transition queried across changed snapshots, the same-day politeness gate not touching the network, forced re-ingest idempotency, the v9→v10 migration, and the `run-due` hook firing on zero-due nights without ever failing one
  — the suite stays hermetic via an autouse stub of that hook)
- Driving-plan join (every country alias and admin-suffix form, day-first dirty-date recovery and its `window_approximate` flag, tier precedence, a **stale manual link falling through** instead of stranding a city, the verdict vocabulary including Israel's `driven_unplanned` end-to-end, records regrouped from exploded districts, a **country-tier match not claiming a record covers the city**, and every implausible capture date being suppressed *and* unable to produce a `driven_unplanned`
  — plus an autouse scheduler stub, since the tail regenerates this artifact unconditionally and `data_dir` defaults into the working tree)

## Capture dates (issues #213 and #226)

- Capture-date narrowing (issue #213: an impossible future date and a pre-launch one dropped from every dated statistic
  — through **both** the catalog and per-run-JSON seams, since they are separate call sites
  — while the pano/coverage totals still count that imagery; the floor being provider-specific rather than shared; the ceiling being *inclusive*,
  or every city collected the day it was driven would lose its freshest imagery; the gsv columns describing `© Google` only,
  with the archival no-copyright run and every Mapillary run explicitly exempt; the repair count agreeing with what the stats actually dropped; the plan_match floor pinned to the analysis constant since the pure-logic module keeps its own literal; and the recompute script end-to-end,
  where the JSON rebuild must fire on a **second** pass that finds no stat left to change — it is keyed on the CSV, not on whether the catalog moved)
- Loader's capture-date contract (`tests/test_fileutils.py`, issue #226: month-precision dates surviving the load pinned to the 1st and reaching `calculate_run_stats` as real oldest/newest/median
  — asserted BESIDE the pano counts, which were always right and are why the bug stayed invisible; a mixed-precision file parsing **in both orderings**,
  which is what pins `format="ISO8601"` against the format-free inference that passes one ordering by luck; year precision pinning to Jan 1; and an unreadable date still coercing rather than raising, so one bad row cannot take out a whole run's statistics
  — plus the widened `--regenerate-json` trigger, exercised on a run with **zero** impossible dates, since #213's trigger alone left 3 of the 8 affected runs publishing NULL ages, and exercised **one date column at a time**
  — a catalog row correct in every column but one, which is the case no realistic fixture can produce (oldest, newest and median are a min, a max and a median of one population and move together),
  so a column quietly dropped from `DATE_COLUMNS` would otherwise stop triggering the rebuild forever with nothing to see; with a **non**-date column moving as the control, since a trigger that fired on any change at all would rebuild the whole series every pass.
  The JS mirror is pinned the same way: the three precisions parsing at LOCAL midnight (in a test file that pins `TZ=America/Los_Angeles` before requiring the module, or every assertion passes on a UTC CI runner without the fix), and out-of-range months and days **not rolling over into plausible dates**
  — `new Date(y, 12, 1)` is Jan of the next year, which `isPlausibleCaptureDate` accepts while pandas coerces the same value to `NaT`)

## Frontend node tests

Frontend node tests cover the streetwalk render seam (manifest lookup + fetch-failure fallback, artifact-URL selection, key normalization, the fractional ramp, and initial view mode) by stubbing Leaflet and the panel's DOM, and `panoDateOrNull`'s reduced-precision shapes (issue #226), asserted on the LOCAL getters rather than `toISOString()`
— a UTC-built date and a local-built one agree on the ISO string in exactly the timezone that hides the bug, so an ISO comparison passes everywhere and protects nowhere.

## The pivoted data tables (issue #250)

**The pivoted data tables (issue #250) are covered on both sides.**
Node: `theadHtml`'s group-free output compared against the expression it replaced rather than a hand-copied string (the driving.html guarantee), the two-row form's colspan/rowspan and leaf-only `data-key`, and the first-VISIBLE-member group label;
`histogram-range` asserted against its plain-`range` TWIN in unset/pass/parse/serialize rather than against typed expectations, since the point is that the two are one value shape;
`rowsExceptFilter` ignoring self while the search box still narrows;
`histogramBuckets`' fixed domain clamping out-of-range values into the end buckets instead of dropping them;
the select `defaultValue`'s four cases (absent, unknown, blank, explicit) and `defaultFilterValues` as what "Clear all" resets to;
`sliderStepFor` never returning zero on a degenerate domain and `normalizeSliderRange` turning a full span into nulls, swapping crossed handles and clamping to a domain that need not start at zero;
`classifyBuckets` dimming by OVERLAP, so a bucket cut by a handle still counts;
the union-not-intersection pivot with a provider-absent city reading null rather than zero;
Δ null-unless-both in both directions;
the shared-geometry collapse asserted in BOTH provider orders, so "first non-null wins" is not "first provider wins";
a synthetic THIRD provider getting keys, a filter option and a `pctBest` contribution but no Δ;
and the per-provider link cells never taking the bare-`city_id` name fallback's filename;
and for the scope, `resolveFilters` in all four states (unscoped reads the best-across field, scoped reads that provider's, the 2+ option scopes to no single provider so it stays best-across, and a descriptor with no scope hook comes back by IDENTITY), the composed query that used to return 56 wrong rows asserted as returning none,
a scoped BOOLEAN resolving its `test` rather than only its wording, and
— the one that catches a typo'd `base`
— **every field a scoped filter can resolve to asserted to exist on a real row model**, for every provider in the registry, since a field that resolves to nothing filters nothing and looks like a working control.
e2e: one row per city with a populated Δ and an absent-provider em-dash beside it, a Δ header click, the network selector's round-trip including a cold `?network=all_public` reload, a keyboard-only `ArrowRight` moving the rows AND the precision input AND the URL together, the crossfilter rule (a search redraws a slider's bars, its own brush does not),
the sidebar beside the table at 1440px and collapsing at 600px and **re-opening on widening**, the column picker's self-contained `pickerLabel`s and the header collapsing back to ONE row when every optional column is unchecked;
every per-provider cell of a row opening THAT provider's series while the Δ cells open nothing and a provider with no run here stays plain;
and the layout asks themselves as outcomes rather than word counts — the table's first row and the search box both above the fold at 1440×900, the long prose present but closed, and the sidebar sticky, painted, and taller than 85% of the viewport
— all four of those layout tests parametrized over **all three** table pages once driving.html joined the sidebar (see below), so a page cannot quietly opt out of the shape;
and the scope end to end in a browser
— the unscoped legend naming its quantifier, a scope change clearing the window from all three of its carriers at once (rows, URL, precision input), the axis re-seeding to the scoped provider's range, both labels following it, a scoped window returning no rows where best-across would have returned a city whose own Mapillary number contradicts the filter,
and a `?provider=&age=` link surviving a cold load rather than being cleared on arrival.
The registered-vs-collected narrowing is pinned from both sides: that a registered provider with nothing in the payload gets no leaf column, no preset entry and no scope option, while the SAME build given a payload that does carry it gets all three
— so the fix cannot be mistaken for deleting the fan-out
— that the Δ columns and the Δ filter disappear together when only one of the pair is collected while the row's Δ fields still read **null rather than missing**, and that the default preset's width grows by exactly one leaf per grouped metric per collected provider, which is the layout budget the presets are sized against.
The two column/model seam tests (`every sortable column key exists on a row model`, `every scoped field a filter can resolve to exists on a row model`) now run over BOTH a narrowed and a full-registry payload, and read their columns and filters from the payload's own build rather than from the module-level one
— asserting a narrowed row model against the full-registry descriptors asks for keys the pivot deliberately does not create.
`createHistogramSlider` itself is now driven offline against a hand-stubbed root element rather than only through the browser e2e: its helpers were each pinned but their COMPOSITION was not, and the composition — raw extent → step → snapped domain → is the top of the data a reachable value?
— is where the defect this control was rebuilt around lived.
Six domains, including the three measured ones, a domain spanning zero, a sub-unit one and a degenerate one, each asserted to contain the data, to land on a whole number of steps (which is what makes both ends reachable at all), to overshoot by less than one step, and to agree with the `min`/`max`/`step` attributes the browser actually snaps against
— the property, rather than one fixture's incidental span.
Beside it: that `setDomain` hands back the SNAPPED axis rather than echoing its argument (an echo is exactly the two-axes bug), that a re-seeded axis re-normalizes the window it is holding, and that `destroy()` really aborts the one signal every listener was attached with — which is also what stops that method being untested API surface.
Three smaller pins: a grouped leaf's header button carrying `pickerLabel` as `aria-label` **while a descriptor without one emits no `aria-label` at all** (driving.html declares none, and its header markup must not move);
`walkChangeCellHtml` rendering an exact zero unsigned and grey, matching `deltaCellHtml` one column over, while still signing a real direction;
and `updateStreetsCaption` formatting its counts at a scale where the separator shows, since the fixture's own counts are single digits and would pass either way.
And that aggregate records with no `city_id` stay DISTINCT rows with a warning rather than collapsing into one shared "Unknown" — latent, since the published v3 aggregate always carries one, which is exactly why it needed a test rather than a reader's trust.

### driving.html joined the sidebar (issue #188 follow-up)

**The four shared layout e2e tests are parametrized over all three table pages** — that parametrization is described with the rest of the #250 e2e list above, and this is the entry that says WHY driving.html is in it: the pages are one shape, and a page that drifts off it now fails a test another page wrote.
driving.html is the tightest case on the first-screen budget, since it renders the archive-provenance callout above the layout as well as the page head, which is exactly why it is worth having in the list rather than exempted from it.

**One test stayed page-specific, and its subject changed:** `test_driving_takes_the_sidebar_without_taking_the_pivot`.
driving.html shares the chassis, the sidebar and the histograms but NOT the pivot — its rows are places, so no column declares a `group` — which makes it the only page rendering `theadHtml`'s flat single-`<tr>` branch **by default** in a real browser, and the only place a Δ-cell count of zero means anything.
That branch is a data-shape rule (`!visible.some((column) => column.group)`), not a driving.html feature, so it is emphatically not driving.html's to own: `test_unchecking_every_optional_column_actually_empties_the_table` already watches grid.html collapse to the same single `<tr>` with zero `th.th-group` once every grouped column is unchecked.
Saying "only caller" here would invite a later PR to delete the branch along with a pivoted driving.html, and break grid/streets in a state only the advisory e2e job covers.
It replaced `test_distribution_strip_survives_on_the_driving_page`, whose premise (the strip was removed from two pages, not from the chassis) stopped being true — and the replacement's **header-click assertions had to be rewritten to be positive ones**.
The strip was what made a driving.html header click observable, because it retargeted itself at the newly sorted column; with the strip gone site-wide, asserting its absence after a click says nothing about the click, and inert header buttons passed.
It now asserts `aria-sort` moving to the clicked column and off the old one, the `?sort=`/`?dir=` pair in the URL, and the first row actually changing between the two directions.

**Tests were deleted only where their subject was**, and it is worth being explicit about which:
`bucketCountFor`, `medianOf`, `formatStripSummary` and the three `renderDistributionStrip` cases went with the strip itself.
The `histogramBuckets` cases were rewritten rather than dropped — the helper survives, but its axis is now the caller's in every case, so the self-scaling assertions became fixed-domain ones and the "without a domain, nothing moves" test had nothing left to say.
`controlsHtml`'s inline-order test went with the `layout` option; the sidebar-order test lost its `layout: "sidebar"` argument and kept every assertion, and it is still worth reading for the reason its fixture declares filters in a DIFFERENT order than they render in.
`controlsHtml: renders no sorted-column distribution strip` went too, for the opposite reason to the rest: its subject was already gone, and a `doesNotMatch(/distribution-strip/)` against a file that no longer contains the string cannot fail.
So did the two `histogram-range` PARITY tests, which asserted `f(hist) === f(plainRange)` at every value-shaped call site — every one of those sites dispatches through `isRangeType` first, so both arguments took the same branch and the comparison was vacuous.
Their content survives as typed expectations (the `min~max` wire format written out rather than compared), and the shared fixture in `table-controls.test.js` now declares `histogram-range`, the only numeric-window type left.

**Added:** `DRIVING_FILTERS: numeric filters are histogram sliders over real row fields`, the contract grid.js and streets.js already carried.
It asserts the field exists on **both** driving row kinds — a tracked city and a plan area — because the axis is seeded from the full row set, and a field present only on cities would read `undefined` off every untracked area without failing anything.
It also pins each filter's `unit` and `digits`, which `histogram-slider.js` is the only reader of and which the plain `range` renderer these three came from never read: without them a thumb on the 0–12 yrs axis announces "0", "0", "1" for 0.2/0.4/0.6 while the column renders `0.2 yrs`, and every bucket tooltip reads "0–0: 12 rows".
The type half of that assertion got MORE load-bearing when `type: "range"` was deleted: a descriptor left saying `range` no longer renders two working number inputs, it falls off the end of `controlsHtml`'s type partition and renders as a **checkbox** for a `{min, max}` value.

**Added:** `test_a_page_with_nothing_published_shows_no_empty_sidebar`, parametrized over each page and the artifact that page cannot render without.
The empty state is a real state of a real deployment — driving.html before its first `fetch-driving-plan`, streets.html before the first road walk, plus a transient 404 on any of the three — and the sidebar being an unconditional `position: sticky` full-viewport-height white card meant a viewport-tall empty panel beside a one-line "nothing published yet", with an `aria-label`led landmark holding nothing.
It asserts the **attribute**, `hidden`, and the computed `display: none` separately, and both halves were confirmed to fail on their own: an unrendered layout has no content, so it collapses to zero height and Playwright's `to_be_hidden()` passes either way, and `display: grid` is an author declaration that beats the UA stylesheet's `[hidden]` rule unless `.table-layout[hidden]` says otherwise.
The reveal lives in `createTableControls` rather than in the three pages, because every empty/error path returns before reaching it — so the page that is added next cannot forget it.

Two more chassis-level pins came with the same work.
`foldSearchTerms` is asserted to agree with `matchesSearch` on the same queries, because folding the query once per pass instead of once per row is only an optimization if the two spellings cannot diverge.
And the per-row search haystack cache is asserted to be keyed by the **field list**, not just the row object — a cache ignoring the field list would serve one caller's haystack to a caller asking about different columns, which is a wrong answer rather than a slow one.

