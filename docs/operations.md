# Operations: same-day assessments and publishing

Operator-facing commands and the publish path. Read before touching `assess-city`, `_publish`, or
anything about how the site gets its files.

Split out of `CLAUDE.md` (2026-08-22); the router keeps this topic's short rules and points here for the evidence and detail.
An edit that changes a rule belongs in both files; anything written since the split is under its own heading and says so.

## Answering a partner inquiry the same day: `scheduler assess-city "City, Region"` (issue #215)

**Answering a partner inquiry the same day: `scheduler assess-city "City, Region"` (issue #215).** A Project Sidewalk deployment inquiry arrives by email about a city we don't track, and the useful reply happens *that day*.
One command registers the city, runs both **road walks** plus the Mapillary **grid** run, regenerates the published JSON, publishes, and prints the numbers.
Four things about it are load-bearing.
**(1) Answer from street coverage, never grid coverage.** The NKY round measured Highland Heights at 55.6% of grid points but **92.8% of street-km**, and Covington at 8.2% vs **50.8%** on Mapillary
— grid points land on river, rail, parkland and rooftops, so a grid percentage badly understates what a deployment would get.
`_assess_answer_report` therefore leads with street-km and labels grid coverage in the output as an area measure that is *not* the deployment number.
**(2) The channel set is `gsv_streets` + `mapillary_streets` + `mapillary`, and the GSV grid run is excluded on purpose.** The walks are the answer and the cheap half (a Mapillary walk is 12–180 tiles for a compact city; a GSV walk is per-project-metered with no per-IP exposure).
The Mapillary *grid* run is in the set for a different reason: it is the **same census the walk already pays for**, and it is what makes the answer *linkable*
— `generate_aggregate_v2` skips a city with no `runs` row, so a walk-only city is absent from `cities.json.gz`, and `city.html` is addressed by run-CSV filename, so `streets.html` would show the walk under a raw slug with nothing to click.
The link nevertheless **falls back to the GSV run** when there is no Mapillary one, and is labelled with the provider it opens: that channel is routinely absent here (switched off after a per-IP block, narrowed away by `--provider`, over budget, or skipped by the breaker), and an already-tracked city has a GSV run, so asking only about Mapillary reported "no city page" while a working one existed
— and sent the operator away to wait for a nightly batch that had already run.
The **grid-coverage figure** stays Mapillary-only even then, deliberately: GSV grid coverage is precisely the number this command exists to stop anyone quoting.
The GSV grid run needs no help arriving: a newly registered city is `enabled` with `last_success_at` NULL, which puts it at the head of the next night's stalest-first queue.
**(3) A rectangle is not a city, and the pre-flight says so before anything is spent.** `boundary_audit.rect_in_boundary_frac` (the reciprocal of the existing `rect_polygon_coverage`, built on the same shapely-free shoelace math) reports what share of the sampled rectangle is actually inside the boundary;
below 0.70 it warns and names the precedent.
This is not hypothetical — the four NKY county rectangles scored **49–69%**, and the out-of-county remainder was largely **Cincinnati**, whose dense recent GSV would have flattered every figure quoted to the partner.
Newport, KY scores **46%** on today's geometry.
The probe is one unlocked Nominatim call wrapped in `except Exception` and is **advisory only**: like the Overpass `/status` probe, it must never be able to fail the work it speaks for.
**(4) It reuses the nightly machinery rather than reimplementing it.** `_run_city_channels` was extracted from `_run_city_loop`'s inner per-channel body, so both callers share one copy of the host breaker, both budget guards, the resource guard, orphan salvage and cadence bookkeeping;
`_run_city_loop` keeps only what is genuinely about a batch (city cap, deadline, inter-city sleep).
Three deliberate differences, all parameters: `batch_deadline=None` (an operator run has nothing queued behind it), `stop_requested=None` (issue #206
— a foreground command is interrupted with Ctrl-C, not by a supervisor stopping a unit, and there is no batch behind this city to wind down; required rather than defaulted for the same fail-open reason as `batch_deadline`), and `record_failures=False`
— a success *is* recorded, because that is what stops the next nightly batch re-spending the same crawl hours later, but a failure is not, since `get_due_cities` filters on `consecutive_failures < max_consecutive_failures` and **nothing resets that counter except a success**, so letting an ad-hoc probe increment it would let a few of them quarantine a city for a whole cycle.
**That recorded success has #214's paired-snapshot cost and the closing report names it**, because the natural thing to say there is the opposite of true: after a clean run the collected channels are the *least* stale rows in the catalog and are **not** due tonight, only `gsv` is (it never got a `schedule_state` row)
— so a city assessed this way stops sharing one run date with its own channels until the cadences re-converge.
A test asserts the wording against `get_due_cities` rather than against the sentence alone, so the two cannot drift apart again.
**There is deliberately no `--publish` override, unlike `regenerate-aggregate`'s**
— `[publish].enabled` is the host's own declaration, and moving publishing out of ambient state and into config is what the rest of #215 does, so an override belongs to the command whose job genuinely *is* "push the catalog to the site right now" (the incident-time handle after a died batch) and not to a collection command whose publish is a consequence.
It would also be the one flag letting a non-prod checkout overwrite prod's `cities.json.gz`, which `_regenerate_published_json` rebuilds from the **local** catalog.
What the absence needs instead is a **notice**: with publishing off, the printed city-page link describes the catalog and reads exactly like an answer while pointing at stale or absent data, and the only other signal was the *absence* of `; published` from the summary line.
The realistic victim is not a dev laptop but prod with publishing switched off during a block or a maintenance window.
Exit stays 0 there, on the same reasoning that makes `--no-publish` exit 0 — only an *attempted* publish that failed is a failure.
Refusals mirror #214's: an unpaired `--width/--height`, a `--provider` naming the grid channel or an unknown/disabled one, and a config with no assess channel enabled all exit `USAGE_EXIT_CODE` **before the catalog is opened**.
`--width/--height` without `--lat/--lng` is refused where `cli.py` merely tolerates it, because size alone freezes the grid on the OSM bbox midpoint rather than downtown — the right size in the wrong place, permanently.

## Publishing is declared in config, not inherited from the environment (`[publish].local`)

**Publishing is declared in config, not inherited from the environment (`[publish].local`).** `sync_data_to_server.sh` has always accepted a local-rsync mode, but the scheduler only ever reached it via `STREETSCAPE_PUBLISH_LOCAL=1`
— which the systemd unit exports and an operator shell does not.
So any hand-run publish on makelab2 (`regenerate-aggregate --publish`, and now `assess-city`) took the SSH path, failed with rsync code 12, and had `_publish` email a **publish-FAILED alert that reads as an outage**.
`[publish].local = true` (set in `scheduler.makelab1.toml`) makes `_publish` pass `--local` explicitly, so the two invocation paths are identical;
the unit still exports the variable, harmlessly, so a code rollback cannot break nightly publishing.
`[publish].site_url` is used for nothing but printing operator-facing links.

## Landing a laptop investigation in this catalog: `scheduler import-bundle` (issue #330)

**Added after the 2026-08-22 split.**

An inquiry about an untracked city arrives and the useful window is the next hour, not the next night.
`assess-city` above answers it, but only from prod — so it competes with the nightly batch for the daily ledgers and, more importantly, for prod's **per-IP** allowance on the three metered hosts, and it can only run outside the batch window.
Investigating from a laptop avoids all of that and needs no new code: every collector takes `--download-dir`, so a scratch directory becomes a complete, self-describing investigation.
What `import-bundle` adds is the way back.

```bash
# 1. Investigate on the laptop — ordinary collector commands, one per provider
python -m streetscape_street_analyzer.collect "City, Region" --provider kartaview --download-dir /tmp/city/data

# 2. Ship the bundle up. archive/ is gitignored and outside the publish rsync's walk,
#    so a staged bundle is structurally unpublishable.
rsync -a /tmp/city/data/ makelab2:~/streetscape-tracker/archive/investigations/city/

# 3. Land it. Dry run is the DEFAULT; it prints exactly the plan --execute carries out.
scheduler import-bundle archive/investigations/city
scheduler import-bundle archive/investigations/city --execute --enable
```

A "bundle" is not a new format — it is exactly the `data/` directory a laptop run already wrote, catalog included, and the importer reads it with a **read-only** connection so it can neither migrate nor mutate the operator's evidence.

**Why the laptop is the right place rather than merely a convenient one.**
Three of the four providers meter by IP address, not by credential (`docs/provider-access.md`), so moving the work to a laptop isolates it for free and needs no new token.
GSV is the exception and cuts the other way: Google meters per Cloud **project**, so a laptop walk on `GMAPS_STREETS_API_KEY` draws on exactly the pool prod's own walks draw on — at the collector's default 24,000/min, on top of prod's.
Pace laptop GSV work explicitly whenever the batch may be running, and note that a run ≥95% `OVER_QUERY_LIMIT` is rejected before cataloging, so the damage would land on prod's in-flight city rather than on the investigation.

**Every refusal happens before anything is written, and rejects the bundle whole.**
Half an investigation in the catalog is worse than none: `db.add_api_usage` is additive rather than idempotent, so a partial import that the operator retries would double-charge the ledger.
The collision refusal is what makes a retry safe, and it only makes it safe if the first attempt wrote nothing.
The refusals are: a bundle on another schema version, a live `-wal` beside its catalog, a provider this checkout does not know, a `city_id` this checkout derives differently from the same name parts, a geometry that disagrees with this host's frozen grid, or a filename this host's generators would not produce;
a run or walk that already exists here (on its composite key OR on `csv_filename` alone, which carries its own `UNIQUE`), or one dated at or before this host's newest of that series;
an artifact a row names but the bundle lacks, a destination file already on disk, or a frozen network whose bytes differ from this host's;
a walk row that disagrees with its own coverage artifact or sample count; and a batch that appears to be in flight.

The geometry check is the load-bearing one, and it did not exist anywhere before this: every `naming.same_grid_geometry` call compares filename to filename, never a filename to the `cities` row.
A bundle collected on a different rectangle is not a later snapshot of the same series, it is a different series wearing the same `city_id`, and nothing downstream would say so.

**A series is append-only, and an import that extends one gets its diff.**
The collector only ever adds the newest run of a (city, provider) series and diffs it against the one before, so every diff on this host describes two adjacent runs; a bundle run dated at or before this host's newest for that provider would slot into the middle of the series, behind a diff that assumes adjacency, and is refused.
An imported run that extends a series is diffed here by the collector's own `_compute_and_record_diff` (and a walk by `compute_and_record_walk_diff`), behind the collector's own `same_grid_geometry` gate — the previous run may predate a catalog-only resize or be an archival baseline, and a cross-geometry diff would render as imagery churn — because the bundle's catalog knew only the laptop's runs and `regenerate_run_json` replays a `run_diffs` row rather than computing one — without the row the JSON's change block is null and `city.js` falls back to constructing the detail filename from run history, a 404 on the site.

**A frozen network this host already holds — cataloged or merely on disk — is never replaced.**
The GraphML name is deterministic per `(city_id, network_type)` — no date, no host token — so a bundle's network always names the SAME file this host has; the bytes decide, and `osm_cache/` is looked at here because it sits outside the artifact sweep.
Identical: this host's row and file stay and nothing is written for it.
Different: the bundle's walk was measured on another OSM snapshot than this host's series, and replacing the file would silently re-base every walk already cataloged, so the bundle is refused — a row-less file with different bytes refuses too, since nothing records its provenance.
A network new to this host lands with its own `fetched_at` carried — that is when the OSM snapshot was taken, the provenance a frozen network is judged by.

**Two asymmetries are deliberate.**
A grid run's stats are **recomputed** from the copied CSV — one pandas pass through `analysis.calculate_run_stats`, the same call the collector makes — so a stat definition that moved between the two checkouts cannot enter the series as an unattributable step change.
A road walk's are **carried** from its row and cross-checked against the coverage GeoJSON's own totals and the CSV's row count, because recomputing one means re-running the OSM edge join for a result that can only equal what the artifact beside it already records.
Second, spend is ledgered only for credentials this host shares — `gsv`, `gsv_streets`, `kartaview`, `kartaview_streets` — under the bundle's own date.
Charging the per-IP channels here would tighten a budget gate against requests this host never made, on the very channel whose budget is the instrument in an open block investigation.

**The cadence write is the point of the whole exercise.**
Each imported channel gets `db.record_attempt(success=True)`, the same call `reconcile-walks` makes after a salvage, so the next night does not re-collect what the laptop already paid for.
It is written inside `apply_bundle`, AFTER every row a retry would refuse on and BEFORE the ledger, so a crash at the ledger leaves a refused retry and a city that is still not due — written after the ledger, the same crash left a refused retry and a city the next night re-collected.
The clock starts at import time, not at the bundle's run date: a bundle imported N days after it was collected pushes that city's next collection out by N days, which is a wash for a same-week landing and worth knowing for an older one.
A walk stamps its channel only when its `network_type` is the one the channel is configured to walk (`[providers.<channel>].network_type`, default `drive`): each type is its own series, so an `all_public` walk says nothing about whether the channel's `drive` walk is due.
A channel with no scheduler arms yet (anything in `UNWIRED_CHANNELS`, empty today) is skipped rather than recorded, since a success there would suppress its FIRST real collection once the channel is wired.
As after `assess-city`, the imported channels are then the *least* stale rows for that city and are **not** due tonight — the closing report says so, because the natural assumption is the opposite.

Three pieces of #330 are deliberately still open: a laptop-side `investigate` driver that runs the collectors and does the rsync itself, a `register-city` subcommand so the laptop asks prod for the frozen grid *before* collecting rather than being checked against it afterward, and a pidfile written by `run-due` to replace the batch check's `ps` heuristic.

## Keeping Overpass out of the night: `scripts/prefreeze_street_networks.py` (issue #341)

A road walk on a frozen network never contacts Overpass — `fetch_graph` returns the cached GraphML before it takes the host lock or probes — and only a city's *first* walk fetches one.
So a daytime pass that freezes the networks of tonight's walk cities takes nearly all of Overpass out of the nightly window, and turns a mid-night refusal from "strands the rest of the night" into "affects the few cities the pass had not reached".
It moves existing fetches earlier; it adds none.

```bash
# What tonight's walks would fetch (dry run is the default; nothing is requested):
python scripts/prefreeze_street_networks.py --config config/scheduler.makelab1.toml

# Freeze them, serially, two minutes apart, through the same lock and /status probe a walk uses:
python scripts/prefreeze_street_networks.py --config config/scheduler.makelab1.toml --execute

# Two nights ahead, at most 30 fetches:
python scripts/prefreeze_street_networks.py --config config/scheduler.makelab1.toml --nights 2 --limit 30 --execute
```

It predicts the slate the way `run-due` builds it — `_collect_due` over the enabled channels, hoist and refresh reserve included, for **tomorrow's UTC date** (what the 02:00 Pacific timer fire reads; `--date` overrides) — and keeps the cities inside the cap that are due on a street channel and have no frozen GraphML for that channel's `network_type`.
`--nights N` widens the window to N caps' worth of the stalest-first order, an approximation twice over: each night re-resolves its reservations, and a city whose every channel is skipped does not consume a cap slot, so a real night reaches past the first `max_cities_per_day` entries — `--nights 2` covers both.
It stops at the first host refusal or busy lock and exits with that host's code (76 / 80), exactly as a collection child does; a bbox with no drivable ways is logged and the pass continues.
It **refuses to run beside an in-flight `run-due`** unless `--force`, checked before *every* fetch rather than once — a pass is long, the timer does not wait for it, and the walk that then loses the Overpass lock exits busy and strands its city (#341) — so run it in the daytime, clear of the timer.
This is the one script in `scripts/` that makes provider requests, and it is dry-run by default for that reason.
There is no timer for it yet; scheduling it is a pacing decision to take against the Overpass usage policy first (CLAUDE.md, READ THIS FIRST).

**Recovering cities a refusal already stranded.**
The alert names them, with the command: `scheduler run-due --provider gsv_streets --limit N` (or the Mapillary/KartaView walk channel) once Overpass is confirmed serving prod — `python -c "from streetscape_metadata_tracker.download_common import overpass_serving; print(overpass_serving())"` from the checkout on the host must print `True`, which is the breaker's own re-check: one tiny, metered `/api/interpreter` query sent the way a walk sends it (#356).
A `curl .../api/status` answering 200 with a slots line is **not** that test — on 2026-09-21 `/status` said serving twice while the next real query was refused.
Their walks will carry a later date than their grid runs, so they stay un-paired either way; a filtered run advances only the named channel's clock.
