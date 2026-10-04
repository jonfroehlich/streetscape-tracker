# Operations: same-day assessments and publishing

Operator-facing commands and the publish path. Read before touching `assess-city`, `_publish`, or
anything about how the site gets its files.

Split out of `CLAUDE.md` (2026-08-22); the router keeps this topic's short rules and points here for the evidence and detail.
An edit that changes a rule belongs in both files; anything written since the split is under its own heading and says so.

## Before any hand run or catch-up (check the server and allocations)

**Added after the 2026-08-22 split** (#304, PR #399 review, #412).

Claude runs essentially every hand run and catch-up on prod (makelab2), so this checklist is the mitigation for the overlaps the code does not refuse.
Since #304 each GSV process actually reaches its configured pace, so a hand run on the **same GSV key** as a nightly lane that is collecting presents more than that project's 60,000/min quota: ~72,000/min for a direct-CLI run at its 24,000 default beside the lane's 48,000, and ~96,000/min for a scheduler-path run, which paces at the lane's own 48,000.
The nightly `gsv` and `gsv_streets` lanes never collide with each other: they use different keys in separate Cloud projects.
The decision taken is **no lock and no shared pacer**: hand runs follow this checklist.
Since #412 a hand `run-due` or `assess-city` on a GSV key another `run-due` on this host is collecting exits 64 (`--force` overrides), and the nightly is never refused but alerts; that guard is per-host, sees only `run-due` processes and checks only at start, so a direct-CLI run and a run on another machine still rely on this checklist (blind spots in `docs/scheduler.md`).
The mechanism, the realistic pairs and the rejected options are in [`provider-access.md`](provider-access.md) (the #304 section); this section is only the procedure.

### 1. Gather everything in one SSH call

The makelab hosts ban an IP that opens connections too fast, so reuse ONE control master and batch the checks into one call.
Before connecting, look for a live master (`ls ~/.ssh/cm-*`, then `ssh -O check makelab2`); open one with `ssh -fNM makelab2` only if none is live.
**If a connection times out once, stop**: no retry, no longer timeout, no hop through makelab1, and tell the operator, since a ban and a hung host look the same from outside.
The checkout is `/projects/makeabilitylab/streetscape-tracker` (`~/streetscape-tracker` is a symlink to it), and prod runs from `.venv-makelab2` with `config/scheduler.makelab1.toml` ([`deploy/README.md`](../deploy/README.md)).
The snippet only reads: `ps`, `systemctl --user`, the scheduler log, `scheduler status` and the config.

```bash
ssh makelab2 'bash -s' <<'EOF'
cd /projects/makeabilitylab/streetscape-tracker || exit 1
PY=.venv-makelab2/bin/python
CFG=config/scheduler.makelab1.toml
LOG=logs/streetscape_scheduler.log
echo "== $(hostname) at $(date -u '+%F %T') UTC ($(TZ=America/Los_Angeles date '+%T %Z'))"

echo "== units (active = running now)"
for u in streetscape-tracker streetscape-prefreeze streetscape-screen-provider streetscape-backup-check; do
  printf '  %-36s %s\n' "$u.service" "$(systemctl --user is-active "$u.service" 2>&1)"
done
systemctl --user list-timers 'streetscape-*' --no-pager 2>&1 | head -n 7

echo "== run-due in flight (empty = none)"
ps -eo pid=,etime=,args= | awk '/streetscape_metadata_tracker[.]scheduler/ && /run-due/'

echo "== collection children in flight (empty = none)"
ps -eo pid=,etime=,args= | awk '/streetscape_tracker[.]py|streetscape_street_analyzer[.]collect/'

echo "== deploys, repair scripts, other scripts (empty = none)"
ps -eo pid=,etime=,args= \
  | awk '/scripts\/[A-Za-z0-9_]+[.]py|deploy_makelab1[.]sh|git[ ]([-]C[ ][^ ]+[ ])?(pull|fetch|merge|checkout|rebase|reset)|uv[ ]pip[ ]sync/'

echo "== scheduler log: latest launches, then the tail"
grep -E 'Collecting ' "$LOG" | tail -n 8
tail -n 5 "$LOG"

echo "== budgets (per UTC date) and per-IP host windows"
"$PY" -m streetscape_metadata_tracker.scheduler --config "$CFG" status 2>&1 \
  | grep -E 'budget today:|rolling 24 h:|due today|Error|Traceback'

echo "== GSV paces and the batch deadline, from the deployed config"
"$PY" - "$CFG" <<'PYEOF'
import sys, tomllib
c = tomllib.load(open(sys.argv[1], "rb"))
print("  gsv         [download].max_requests_per_minute             =", c["download"]["max_requests_per_minute"])
print("  gsv_streets [providers.gsv_streets].max_requests_per_minute =", c["providers"]["gsv_streets"].get("max_requests_per_minute", "unset: falls back to [download]"))
print("  [schedule].max_batch_hours =", c["schedule"]["max_batch_hours"])
PYEOF
EOF
```

The `awk` patterns spell `.`, `/`, `-` and spaces as `[.]`, `\/`, `[-]` and `[ ]` so they cannot match awk's own command line, which a plain `ps | grep` does.
The slash is escaped rather than bracketed because `[/]` inside an awk regex literal is a syntax error in BSD awk ("nonterminated character class"), though mawk and gawk accept it.
The git alternative allows `-C <dir>` because `deploy_makelab1.sh` runs `git -C "$REPO_DIR" pull --ff-only`, which a bare `git pull` pattern misses; the script's own name covers its later `rsync` of `www/`.
The run-due line here is a looser two-substring test than the code's: since #412 `scheduler._run_due_in_flight()` (for `import-bundle`, the prefreeze and the two repair scripts) and the `run-due` guard match argv tokens instead, so read any line this prints, and treat one the code would not match (a wrapper's) as the batch it wraps.
The status call keeps `Error` and `Traceback` lines, because a filter for the budget lines alone turns a crashed `status` into empty output.
**Do not use `pgrep -f "scheduler run-due"`**: the unit's command line is `-m streetscape_metadata_tracker.scheduler --config … run-due`, so that pattern never matches it and reports idle mid-batch.
`deploy/README.md` uses `pgrep -af '[s]cheduler .*run-due'`, which does match; the bracket matters, because on Linux `pgrep` excludes only itself, so inside a compound remote command (`ssh host 'pgrep … || echo idle'`) a bare `scheduler .*run-due` matches the parent `bash -c` and never prints idle.

### 2. Read what the batch is doing

- **Is a `run-due` in flight?** Any line under "run-due in flight" means yes, whether it is the nightly unit (`streetscape-tracker.service` active) or a hand-started catch-up.
  The timer fires at 02:00 Pacific and `max_batch_hours = 12` stops it launching cities at ~14:00; the bounded tail (aggregate, manifests, backup, publish) runs past that, so read `ps`, never the clock.
- **Which channels can it still reach?** Read the in-flight `run-due`'s args.
  The nightly unit passes no `--provider`, so it collects **every** enabled channel; a hand catch-up's `--provider` list is the whole of what it can touch.
  The batch is **city-major**: it walks its due cities in order and runs each city's due channels (two at once, in host-disjoint lanes, on prod), so the channel that is live this minute says little about the next one, and an unfiltered night is back on `gsv` with the next city.
- **Which channel is live right now?** The `collect_*` children say: `streetscape_tracker.py --provider gsv` is the `gsv` lane (`GMAPS_API_KEY`), and `streetscape_street_analyzer.collect --provider gsv` is the `gsv_streets` lane (`GMAPS_STREETS_API_KEY`).
  The scheduler log's `Collecting <city> [<channel>]` and `Collecting streets for <city> [<channel>]` lines name the channel of each launch, and each child writes its full output to `logs/collect_<city_id>_<channel>_<UTC date>.log`.
- **Can that GSV channel still spend tonight?** `scheduler status` prints `<channel> budget today: <used> / <budget> requests used.` per enabled channel, keyed by UTC date; a GSV channel at its budget launches nothing more tonight.
  On prod both GSV budgets are 35,000,000, sized so that `max_batch_hours`, not the budget, ends the night, so in practice treat any in-flight unfiltered `run-due` as able to collect both GSV keys until it exits.
- **Per-IP pools:** the same output prints `mapillary_tiles rolling 24 h: <used> / 3,000 requests used.` (`[hosts.mapillary_tiles]`, #385), the pool both Mapillary channels draw from on every scheduler path; read [`provider-access.md`](provider-access.md) before sizing a Mapillary step.

### 3. The GSV keys

| Channel | Env var | Cloud project (account) | Project quota | Nightly pace (config key) |
|---|---|---|---|---|
| `gsv` | `GMAPS_API_KEY` | `gsv-date-tracker`, 634289259936 (UW) | 60,000/min, read in the console 2026-09-15 | 48,000/min (`[download].max_requests_per_minute`) |
| `gsv_streets` | `GMAPS_STREETS_API_KEY` | `gsv-streets-tracker`, 657654513495 (UW) | 60,000/min, the grant approved 2026-09-21, not read back independently | 48,000/min (`[providers.gsv_streets].max_requests_per_minute`) |

Always name the project ID and the account: a decoy project with the same display name exists on another account (the `max_concurrent_channels` comment in `config/scheduler.makelab1.toml`).
The snippet prints both paces from the deployed config, so use its numbers, not this table's, if they ever differ.

### 4. The decision rule

- **Same GSV key as a `run-due` that can still collect it, direct CLI** (`streetscape_tracker.py --provider gsv`, or `python -m streetscape_street_analyzer.collect --provider gsv`): pass `--max-requests-per-minute 12000`.
  12,000 is the project quota minus that key's nightly pace (60,000 − 48,000), so it moves with the config: re-derive it from the pace the snippet printed rather than quoting it.
  It is a ceiling with **no** margin (the two then sum to the quota, and each token bucket can run about a second ahead of its rate), so go lower when the hand run is not urgent.
  Never rely on either CLI's default: both default to 24,000, and 24,000 + 48,000 is already over 60,000.
- **Same GSV key, through the scheduler** (`run-due --provider gsv` or `gsv_streets`, or `assess-city`, whose set includes `gsv_streets`): these take **no** rate override, so wait until the batch can no longer collect that channel (its budget is spent, or the in-flight run is a filtered `run-due` that does not name it), or until the batch has ended.
  The nightly batch is city-major and its GSV budgets do not bind, so against the nightly unit that means waiting until its city loop has ended: the log shows no more `Collecting … [gsv]` / `[gsv_streets]` launches and the tail (aggregate, manifests, backup, publish) is under way.
  Since #412 the code refuses such a run (exit 64) for as long as the other `run-due` is ALIVE, tail included, because it cannot see which channel that process is on; once step 2 shows it is past that key, re-run with `--force`, which is exactly what the refusal message tells you.
- **Different key, or a non-GSV channel:** proceed — for example a hand `gsv` run beside a `run-due --provider gsv_streets` catch-up, or any GSV run beside a `run-due --provider mapillary`.
- **No `run-due` in flight now is not the whole check: the 02:00 Pacific timer starts an unfiltered night on both keys**, and after a reboot the 08:30 watchdog re-arm can start a `Persistent` catch-up night at once ([`scheduler.md`](scheduler.md)).
  Step 1 cannot see a batch that has not started, so decide before launching: a direct-CLI GSV run that could still be going at 02:00 takes the same quota-minus-pace cap from the start.
  A scheduler-path GSV run has no cap, so size it with `--limit` to end well before 02:00; `kill -TERM <run-due pid>` stops further launches but does not signal the in-flight child (only a unit's control-group stop reaches it), so that city still runs to its end.
- **Per-IP providers (Mapillary, KartaView, Panoramax, Overpass):** `host_lock` already serializes them across processes, so a second process fails fast instead of doubling the rate; if the batch's child is the one that loses, its city skips that channel tonight and the night alerts ([`deploy/README.md`](../deploy/README.md), "Running anything by hand alongside the scheduler").
  They still respect their budgets: catch up through `run-due --provider … --limit N` or `--city`, which draw on the daily and rolling-24h ledgers, never a detached script; the staging rules are in [`provider-access.md`](provider-access.md).
- **Never start a catch-up while a deploy or a repair script is running.** A deploy changes the code the next child runs and a repair script rewrites the catalog a run writes into, so the snippet's "deploys, repair scripts" list must be empty; if another session might be deploying, ask the operator first.

### 5. Afterwards

Drive the hand run into a file (`>> logs/<name>.log 2>&1`), never a pipe, and read it when it ends:

```bash
grep -E 'Retry attempt .*quota window reset|points after all retries|refusing to finalize|run rejected' logs/<name>.log
```

**Do not count `OVER_QUERY_LIMIT` in the log**: the engine routes each throttled answer to its retry queue without logging it, so the string appears only in a run rejected as ≥95% denied, and a count of 0 says nothing about a throttled run.
A `quota window reset` retry means a pass saw OVER_QUERY_LIMIT or UNKNOWN_ERROR answers, the first of which is the oversubscription signature (`download_gsv.collect_points_async`).
`points after all retries` means points were still failing after every pass; their statuses are in the run's `*_failed_points.csv` beside its output, and under 1% they are also written into the run's CSV as failure rows.
`refusing to finalize` is the >1% abort (its checkpoint is kept), and `run rejected` is the ≥95% denial that renames the file `*.rejected`.
For a scheduler-launched catch-up, read its `logs/collect_<city_id>_<channel>_<UTC date>.log` instead.
If a nightly lane collected the same key during the hand run, read that night's child logs for the same strings too, because the nightly run is the series whose data matters.
Any hit is a finding: record it in the #304 section of [`provider-access.md`](provider-access.md) with the hand run's rate and the overlap window, rather than lowering a configured pace on one sample.

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
**Do not run it while the nightly batch is collecting** (PR #399 review): its `gsv_streets` walk uses the same key as the nightly `gsv_streets` lane, nothing serializes GSV processes, and since #304 each engine actually reaches its 48,000/min, so the two would present ~96,000/min against that key's 60,000/min project quota.
The same holds for any hand-run GSV collection (`streetscape_tracker.py`, `collect --provider gsv`) against its nightly twin.
The failure is OVER_QUERY_LIMIT answers, retried after 20 s waits and, past 1% of points, an aborted run; the full account and the decision taken (no lock, a pre-run checklist, and since #412 a start-time guard that refuses this command beside a `run-due` on the same key) are in [`provider-access.md`](provider-access.md) (the #304 section).
Run the checklist under "Before any hand run or catch-up" above first: `assess-city` has no rate override, so it waits for the batch to end (and exits 64 if started beside a `run-due` collecting `gsv_streets`), while a direct-CLI hand run can instead pass `--max-requests-per-minute` at the quota minus the nightly pace.
Refusals mirror #214's: an unpaired `--width/--height`, a `--provider` naming the grid channel or an unknown/disabled one, and a config with no assess channel enabled all exit `USAGE_EXIT_CODE` **before the catalog is opened**.
`--width/--height` without `--lat/--lng` is refused where `cli.py` accepts it.
`cli.py` now centers such a grid on the geocoder's reported point rather than the OSM bbox midpoint (#186), but nobody has verified that point is downtown (#185), and an assessment freezes geometry for a partner answer — a guess there is the right size in possibly the wrong place, permanently.

## Publishing is declared in config, not inherited from the environment (`[publish].local`)

**Publishing is declared in config, not inherited from the environment (`[publish].local`).** `sync_data_to_server.sh` has always accepted a local-rsync mode, but the scheduler only ever reached it via `STREETSCAPE_PUBLISH_LOCAL=1`
— which the systemd unit exports and an operator shell does not.
So any hand-run publish on makelab2 (`regenerate-aggregate --publish`, and now `assess-city`) took the SSH path, failed with rsync code 12, and had `_publish` email a **publish-FAILED alert that reads as an outage**.
`[publish].local = true` (set in `scheduler.makelab1.toml`) makes `_publish` pass `--local` explicitly, so the two invocation paths are identical;
the unit still exports the variable, harmlessly, so a code rollback cannot break nightly publishing.
`[publish].site_url` is used for nothing but printing operator-facing links.

## Deploying a stats-definition change

Written for the GSV query radius (issue #367), and the procedure for any change to what a stored stat means.
**The repair is a required deploy step, not an optional follow-up.**
The night after the code lands, every newly collected run is cataloged under the new definition while every older row keeps the old one.
The aggregate (`cities.json.gz`) and the driving page read the STORED `runs` columns, so every re-collected city would show a step change that is only the definition moving — for #367, a phantom drop of about 10% in GSV coverage.

Stop nothing, but finish these before the next 02:00 timer fires:

```bash
cd ~/streetscape-tracker && git pull          # deploy the code; the catalog migrates on first connect
# 1. Dry run: prints every run that would change, and why. Read it before step 2.
.venv-makelab2/bin/python scripts/recompute_run_stats.py \
    --data-dir /projects/makeabilitylab/streetscape-tracker/data --provider gsv
# 2. Apply to the catalog AND rebuild the affected per-run JSONs.
.venv-makelab2/bin/python scripts/recompute_run_stats.py \
    --data-dir /projects/makeabilitylab/streetscape-tracker/data --provider gsv \
    --execute --regenerate-json >> logs/recompute_367.log 2>&1
# 3. Rebuild the aggregate from the repaired catalog and publish it.
.venv-makelab2/bin/python -m streetscape_metadata_tracker.scheduler \
    --config config/scheduler.makelab1.toml regenerate-aggregate --publish
```

`--data-dir` is passed explicitly so the command names the same catalog `[paths].data_dir` names; on prod the checkout's own `data/` is that directory, so the flag is a statement of intent rather than a correction.
`--provider gsv` is not only a filter: `--regenerate-json` re-reads every rebuilt run's CSV, and a census CSV is millions of rows.
Step 2 is a whole-series pass over every GSV CSV, so budget hours rather than minutes; drive it into a file, never a pipe.
If it cannot finish before 02:00, disable the timer for the night (`systemctl --user disable --now streetscape-tracker.timer`, and `enable --now` after step 3; never `stop`, which the #369 watchdog re-arms) rather than let a night catalog runs beside a half-repaired series.

**What the repair does NOT move.**
Historical `run_diffs` rows and the published diff detail CSVs stay under the old definition: `recompute_run_stats.py` re-derives run stats, never diffs, and nothing re-diffs a GSV series yet.
So after #367 a city's "Changes since" panel for an old pair can still count a far pano as added or removed, until a GSV re-diff pass exists (a follow-up).
Diffs computed from the deploy on are correct, because both of their sides load through the new rule.

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

## Enabling a city: one enrolment function behind every path (issue #374)

**Added after the 2026-08-22 split.**

Every path that brought a city into the schedule left the four opt-in channels (`kartaview`, `kartaview_streets`, `panoramax`, `panoramax_streets`) at `schedule_state.member = NULL`, which `CHANNEL_DEFAULT_MEMBERSHIP` reads as "not a member".
So a new city silently collected GSV and Mapillary only, unless an operator remembered four extra `enroll-city` calls — Montréal and Ottawa (2026-09-24/25) were the measured case.
Now every enable path calls one function, `scheduler.enroll_opt_in_channels`, and prints its decisions one line per channel:

| Path | Command | When enrolment runs | Preview |
|---|---|---|---|
| Frame / hand-registered city | `scheduler enable-city CITY` | Before `cities.enabled` flips, so the first night sees every channel | `--dry-run` |
| Laptop investigation | `scheduler import-bundle DIR --enable --execute` | After the bundle lands, only when `--enable` actually turns the city on | The default dry run |
| Partner inquiry | `scheduler assess-city "City, Region"` | After the confirmation, before the walks run, only for a city never touched on an opt-in channel | The pre-flight; `--estimate` stops there |

All three take `--no-opt-in` (skip enrolment entirely; membership is untouched) and `--enroll-kartaview` (below).
`enable-city` refuses an unknown city and an already-enabled one with exit 64: changing an enabled city's membership is `enroll-city`'s job, and re-running the gates on the ~1,200 cities already enabled is the backfill #374 keeps out of scope.
For a city meant to collect on only some channels (the staged rollout in `docs/scheduler.md`), pass `--no-opt-in`, or pre-set `enroll-city --remove` on the opt-in channels too — an explicit value is never overwritten.

**Two gates, one per provider, and a provider's grid and walk channels always move together** — the walk reads the grid's census from the shared cache for 0 requests (#290), so one without the other pays the census twice or never collects.

- **Panoramax** is enrolled only on a **nonzero upper bound** from a one-city screen run at enrolment time.
  It is the weekly `screen-provider` instrument, not a copy of it: `panoramax_screen.screen_targets` over one target, the v2 `grid` layer at z6, under the Panoramax host lock, paced by `_screen_pacing`, and charged to the ledger on the command's own UTC run date.
  A handful of tiles (Newport, KY straddles a z6 seam and costs 2).
  **It writes NO `provider_screen` row**, and decides from the in-memory result instead.
  That table is a series of WHOLE-CATALOG observations: `db.get_provider_screen_series` groups by `screen_date`, and the published summary reports each date's cities-screened and cities-positive counts plus `latest_screen_date`, so a one-city row would publish a point claiming one city was screened that day — the partial screen `cmd_screen_provider` refuses to write.
  The weekly screen records the city on its next pass.
  A screen that is refused (403/429), busy (the lock is `timeout=0`, so it is reported, never waited out), unreadable or failed in any other way enrols **neither** channel and reports `screen_failed` — never "unknown" read as a zero, since a zero is conclusive (#316) and a failure is not.
  Requests actually sent are charged to the ledger on a failure too.
  A failed screen records no opt-in attempt, so the gate is not re-run on its own but CAN be: re-running `assess-city` on that city re-runs it, until something collects the city on an opt-in channel.
  The `screen_failed` line names the remedies: re-check after Monday's `screen-provider panoramax`, then either re-run `assess-city` or run `enroll-city CITY --channel panoramax` and `--channel panoramax_streets` if it is positive.
  `assess-city`, `import-bundle` and `enable-city` still exit 0 on a Panoramax refusal (enrolment is a side decision of each), whereas `screen-provider` exits 84 on the same refusal, so a refusal here is visible only in the report line.
  The weekly screen's catalog-collapse check does not apply to one city (a zero is the ordinary answer for 64% of the catalog); its renamed-layer guard does, so a genuinely empty city spanning two tiles that answer with no hexagon at all reads as `screen_failed`, which is the safe direction.
- **KartaView** is priced with `estimate_kartaview_requests` and enrolled at or below `OPT_IN_KARTAVIEW_ENROLL_MAX_REQUESTS` (1,000).
  #225 measured the median sweep at 16 requests (p95 636), so nearly every city passes; the estimate is a FLOOR (Yogyakarta ran 3.0x it, #248).
  Above the ceiling the pair is `needs_flag`: the estimate is printed and nothing is enrolled unless `--enroll-kartaview` is passed — `--yes` never implies it.

**An explicitly set membership is never overwritten.**
If either channel of a pair already holds a non-NULL `member`, the whole pair is left alone and reported `already_set`, so an operator's `--remove` survives and a pair is never split by enrolling only its unset half.
Enrolment is written through `db.set_channel_membership_pairs`, which shares `enroll-city`'s one upsert and writes a pair's two rows in ONE transaction — two commits could leave half a pair, which `already_set` would then freeze forever.
A channel not enabled in this config is still enrolled, with the same `NOTE` `enroll-city` prints, because enrolment before configuration is supported on purpose.

**What "new" means for `assess-city`: never touched on an OPT-IN channel.**
`db.city_touched_opt_in_channels` is true on any `schedule_state.last_attempt_at` on an opt-in channel, or any `runs` or `street_walks` row from `kartaview` or `panoramax`; such a city is left alone entirely.
It is keyed on the opt-in channels, never on the city's history elsewhere, because `--estimate` registers the city ENABLED: a never-collected city leads gsv's queue, so a 02:00 run between `--estimate` and `--yes` (or after a declined confirmation) stamps a gsv `last_attempt_at`, and an any-channel history gate read the follow-up run as a re-assessment — the Montréal/Ottawa failure, through the documented order.
The consequence is deliberate: re-assessing a long-tracked city that has never been on an opt-in channel enrols it behind the same gates.
`--no-opt-in` is the opt-out, and explicit memberships are handled per pair, so an explicit `--remove` on one pair survives as `already_set` while the other pair is still decided.
Every dry run — `--estimate`, `import-bundle` without `--execute`, `enable-city --dry-run` — writes nothing and issues no provider request, so the Panoramax pair previews as `pending_screen` with the tile count its screen will cost.

**Same-date collection comes from the nightly run, not from `assess-city`.**
`ASSESS_CHANNELS` is unchanged (the opt-in channels stay refusable there, for the reasons at its definition); enrolment is what answers both of those reasons, since the city becomes a member and Panoramax is screened first.
The closing summary says so in one sentence: the grid runs and the opt-in providers enrolled above arrive with the city's first nightly run, which collects every member channel on one UTC date.

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
This is the one SCHEDULED script in `scripts/` that makes provider requests, and it is dry-run by default for that reason.
Since #355 it runs daily from `deploy/systemd/streetscape-prefreeze.timer` at 15:00 Pacific, as `--nights 2 --limit 40 --pause-s 120 --execute --alert`.
The pacing was taken against the Overpass usage policy first (CLAUDE.md, READ THIS FIRST): a regular application should stay under ~100 queries a day, and `--limit 40` is the city cap, so the pass fetches no more than one night's worth while tonight's slate always fits.
It moves the nights' own fetches earlier and adds none; the 10 MB/day half of that figure is exceeded by a large city's network on its own, which is a pre-existing property of road walks rather than something the timer introduces.
`--alert` mails when a pass does not finish — a host condition, a `run-due` in flight, a crash, or a SIGTERM from the unit's `TimeoutStartSec` — naming the networks it left cold, and is silent when nothing was cold.
The one failure it cannot report is an OOM kill, which is a SIGKILL: read `MemoryPeak` rather than waiting for a mail that cannot arrive.
The schedule's rationale and install steps are in [`deploy/README.md`](../deploy/README.md); `tests/test_prefreeze_unit.py` pins them.

**Draining the cold backlog: `--all-enabled` (issue #381).**
The slate mode only ever sees cities *due* by the target date, and a wider `--nights` widens the date window, not dueness — so a cold city is otherwise frozen only the night it is walked, exactly when a refusal strands it (on prod 2026-09-21, 242 of 1,221 enabled cities had no frozen `drive` network).

```bash
python scripts/prefreeze_street_networks.py --config config/scheduler.makelab1.toml --all-enabled            # list
python scripts/prefreeze_street_networks.py --config config/scheduler.makelab1.toml --all-enabled --execute  # freeze 20
```

It plans every enabled city that is a member of at least one enabled street channel and has no frozen GraphML for that channel's `network_type`, stalest-first (never-walked first, then the oldest `last_success_at` among the channels walking that network) with `city_id` as the tiebreaker, so repeated passes make monotone progress.
Membership is read through `get_due_cities_with_last_success` with its staleness gate opened, never a second copy of its membership clause.
Its quarantine gate is **kept** at `max_consecutive_failures`, per channel: a quarantined channel never walks the city until an operator intervenes, so freezing for it buys nothing.
That gate is also what ends a city whose fetch always fails (a bbox with no drivable ways writes no GraphML, so it is cold forever): the nights' own failures quarantine it, and before that it is ordered behind every clean network, so repeated passes move past it rather than re-asking it at the head of each one (PR #382 review).
A pass whose every fetch failed still exits 0 — a city-specific failure is never a failed pass — but prints `WARNING: every planned fetch failed ... NOTHING was frozen`, because each attempt spent a query against the daily budget.
`--limit` defaults to **20** in this mode, so the ~240-network backlog drains over ~12 afternoons by hand rather than in one burst; `--nights` or `--date` beside it exits 64, since neither enters a plan that ignores dueness.
Everything else is the slate mode's code path: serial, `--pause-s` apart, the same lock and probe, the in-flight `run-due` refusal and the stop on a host condition.
Frozen networks are immutable (#103), so this is a one-time cost, and the daily timer stays on `--nights`.
The 20 is sized against the Overpass guidance [`provider-access.md`](provider-access.md) quotes — fewer than ~100 queries a day for an app that queries regularly — which the nightly walks and the daily 15:00 timer (up to 40) already draw on.
The timer runs every afternoon, so a drain **always** adds to that day's count rather than replacing it, and that is why its default is half the timer's.
Before running it, re-read the policy (READ THIS FIRST): do not raise `--limit` or lower `--pause-s` without that check, never run it the afternoon of a daytime walk catch-up, and start it only after that day's timer pass has finished.

**Recovering cities a refusal already stranded.**
The alert names them and prints one `run-due --provider <walks> --city <id>...` per exact set of lost walk channels (#362), pasteable as printed — `--config` is the night's own, and it runs from the project root with the scheduler's venv active.
Run them once Overpass is confirmed serving prod — `python -c "from streetscape_metadata_tracker.download_common import overpass_serving; print(overpass_serving())"` from the checkout on the host must print `True`, which is the breaker's own re-check: one tiny, metered `/api/interpreter` query sent the way a walk sends it (#356).
A `curl .../api/status` answering 200 with a slots line is **not** that test — on 2026-09-21 `/status` said serving twice while the next real query was refused.
Run the commands one after another, never in parallel: a walk whose street network is not yet frozen takes the Overpass host lock, and a Mapillary, KartaView or Panoramax walk takes its census host's lock too, so a concurrent second one can exit busy and skip.
Append `--dry-run` to see exactly which (city, channel) pairs would launch.
A walk is dated the UTC day its command starts, so started the same UTC date as the night (the alert prints it) the walks share the grid runs' date and each pair is kept; from the next UTC day they carry a later date and stay un-paired.
A filtered run advances only the named channels' clocks.
The alert used to print `run-due --provider <walk> --limit N`, which walks the channel's stalest-due queue, not the stranded cities: on 2026-09-22 none of 8 stranded cities was in the first 10 of any walk channel, and Austin's ~640k-request walk led `gsv_streets` (#362).

## Where has a Mapillary contributor mapped lately? `scripts/mapillary_user_activity.py`

**Added after the 2026-08-22 split.**

A laptop-only operator tool for "this user just mapped somewhere — is it a city we track, and do our runs have it?".
It reads one Graph API endpoint (`/images?creator_username=`), never the tile CDN, and follows the `paging.next` cursor so its counts are exact.

```bash
# The last 30 days (the default window), grouped by solar day x 10 km cell:
python scripts/mapillary_user_activity.py uwrapid

# A narrower window, finer cells, a map layer, and production's catalog copied down:
python scripts/mapillary_user_activity.py uwrapid --since 2026-09-01 --until 2026-09-15 --cell-km 5 --geojson /tmp/uwrapid.geojson --db ~/prod-catalog.db
```

Each group reports its solar day, centroid, image and sequence counts, pano share and first/last capture (UTC).
With a catalog, a group inside an enabled city's frozen bbox also carries that city's last Mapillary run date and the newest capture it saw, flagged `AFTER LAST RUN` (captured after it) and `NEWER THAN SEEN` (newer than anything it saw — captured earlier but uploaded later).
A checkout's catalog is usually a dev copy with almost no Mapillary runs, so pass `--db` a copy of production's to answer the real question; the catalog is opened read-only, a schema newer than the checkout's code exits 64, and the report names the path it read (a metrics record keeps only a repo-relative path or a basename).
With only `--until`, the window is the 30 days before it.

The pacing was taken against the provider-access record first (CLAUDE.md, READ THIS FIRST): single-threaded, at least 1 s between requests with jitter that only lengthens a gap, 429/5xx given at most four attempts in all (three retries) on `Retry-After` or exponential backoff, each retry paced like any request, and a 3xx or an HTML page — how Mapillary presents a per-IP block — stops the run at once with exit **75** and is never retried; pages fetched before it are still reported, as lower bounds.
A 403 "Application request limit reached" is the Graph API's per-APP limit, scoped to the credential rather than the IP, and exits 1 like a bad token.
`--max-requests` (default 200, retries included) stops a heavy contributor cleanly with exit **83**; the cursor is newest-first, so a stopped run holds the most recent images and says its counts are lower bounds.
It refuses a `makelab*` host unless `--allow-collection-host`, and nothing in the scheduler calls it.
The measurement behind it, including why the UTC date is the wrong grouping key, is [`experiments/mapillary-user-activity.md`](experiments/mapillary-user-activity.md).
