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
Historical `run_diffs` rows and the published diff detail CSVs stay under the old definition: `recompute_run_stats.py` re-derives run stats, never diffs.
So after #367 a city's "Changes since" panel for an old pair can still count a far pano as added or removed, until the GSV series is re-diffed.
Diffs computed from the deploy on are correct, because both of their sides load through the new rule.

**Re-diffing a GSV series (issue #394) is a pending deploy step, not done and not scheduled.**
The handle exists: `scripts/recompute_run_diffs.py` (#245) loads both CSVs of every existing row through the default `fileutils.load_city_csv_file`, so it inherits the 50 m rule (pinned by `test_a_gsv_rediff_applies_the_query_radius_rule`).
Whether and when to run it on prod is the operator's call; until it runs, the paragraph above describes prod.
Run it only AFTER the #367 stats repair above (`recompute_run_stats.py --provider gsv --execute --regenerate-json`) has completed and its aggregate has been published, so the series' stats and diffs move to the new definition in that order and never the other way round.
A whole-series `--provider gsv` pass also covers the Amsterdam and Auckland rows that #245's phantom-diff repair targets, so no separate `--city` pass is needed for them.

```bash
cd ~/streetscape-tracker
# 1. Dry run: performs the FULL recomputation (both CSVs per row) and prints what would change. Read it before step 2.
.venv-makelab2/bin/python scripts/recompute_run_diffs.py \
    --data-dir /projects/makeabilitylab/streetscape-tracker/data --provider gsv
# 2. Apply to the catalog and the detail files, and rebuild the affected per-run JSONs.
.venv-makelab2/bin/python scripts/recompute_run_diffs.py \
    --data-dir /projects/makeabilitylab/streetscape-tracker/data --provider gsv \
    --execute --regenerate-json >> logs/rediff_394.log 2>&1
# 3. Rebuild the aggregate from the repaired catalog and publish it.
.venv-makelab2/bin/python -m streetscape_metadata_tracker.scheduler \
    --config config/scheduler.makelab1.toml regenerate-aggregate --publish
```

`--provider gsv` matters for the same memory reason as above.
Steps 1 and 2 each load two GSV CSVs per diff row over every series, so budget hours rather than minutes for EACH; drive them into a file, never a pipe.
The script checks for an in-flight `run-due` only when it starts (`--execute` is refused then), and the nightly batch never checks for this script, so a pass still running at 02:00 would race a night that catalogs new runs and diffs beside a half re-diffed series.
If step 2 might not finish before 02:00, disable the timer for the night (`systemctl --user disable --now streetscape-tracker.timer`, and `enable --now` after step 3; never `stop`, which the #369 watchdog re-arms).
The publish never passes rsync `--delete`, so a detail file the pass REMOVES stays on the web server until removed there; the pass lists those names.

### Schema v20 and the `runs.total_grid_points` backfill (issue #289)

v20 is not a stats-definition change — no stored value moves — but it is the first schema step since v16 whose deploy needs the same care.

- **The migration is one-way.**
  Code older than v20 refuses a v20 catalog (`init_schema` raises "newer than this code supports"), so once any process has connected with the new code, rolling the code back stops every command, the 02:00 batch included.
  A rollback is a **catalog restore** (`scheduler restore-backup`, see [`catalog-backups.md`](catalog-backups.md)) as well as a code revert, and it loses every run cataloged since that backup.
- **Deploy only with no `run-due` in flight** (`pgrep -af '[s]cheduler .*run-due'`, as [`../deploy/README.md`](../deploy/README.md) says for any deploy): a child launched from the new tree would migrate the catalog under a parent still running the old module, which then refuses its own next connect.
- **A v19 laptop bundle is refused** by `import-bundle` (it requires the bundle's schema to equal this host's).
  Update the laptop checkout, open its catalog once with any command that connects (that migrates it), and copy the bundle again.

The column arrives NULL on every existing run.
Backfill it with the column-restricted mode, which reads only `query_lat,query_lon` from each CSV and writes only `total_grid_points`:

```bash
# Dry run: one line per run that would change. Expect every run, NULL -> N.
.venv-makelab2/bin/python scripts/recompute_run_stats.py \
    --data-dir /projects/makeabilitylab/streetscape-tracker/data --only total_grid_points --provider gsv
# Apply, one provider per invocation, in the daytime.
.venv-makelab2/bin/python scripts/recompute_run_stats.py \
    --data-dir /projects/makeabilitylab/streetscape-tracker/data --only total_grid_points \
    --provider mapillary --execute >> logs/backfill_289.log 2>&1
```

**Never backfill with a plain `--execute`.**
It loads every CSV through the full loader (the PR #422 review put a 16.5M-row Mapillary census run at ~15 GiB resident, on the host that runs the batch), and it applies every OTHER pending definition change too.
If one of those moves a capture-date column without `--regenerate-json`, that run's published JSON keeps the old dates and no later `--regenerate-json` pass can find it, because nothing moves any more.
So a plain `--execute` that would move any capture-date column is **refused** (exit 64, nothing written), and the refusal lists each affected run as `city_id [provider] run_date`; a plain dry run prints the same list as a WARNING.
The fix is to add `--regenerate-json`; `--allow-unrebuilt-dates` overrides the refusal only for an operator who will rebuild the listed runs' JSONs by hand.
The `--only` dry run's summary line says no other column is read or written; if a plain dry run is what you are reading, it is the wrong command.

Run it **per provider, in the daytime, never overlapping the 02:00 timer** — it shares the catalog with the batch, and a census provider's pass still reads millions of rows apiece.
Nothing published reads `total_grid_points`, so the backfill republishes nothing; `scripts/undated_imagery_share_analyze.py` is its first reader; the backfill ran on production on 2026-10-04 and that regeneration is committed (see [`experiments/undated-imagery-share.md`](experiments/undated-imagery-share.md)).

## Backfilling the Mapillary quality block (issue #321)

#321 adds a `quality` block to every Mapillary run's `mapillary_meta` (the `quality_score` distribution and its on-foot split; [`census.md`](census.md)), and `grid.html` builds its "Imagery quality" group from it.
Runs collected after the deploy get the block on their own; every run summarized before it lacks the block until this backfill splices it in, and the grid page shows em-dashes for those cities meanwhile.
**This is optional and not urgent** — nothing is wrong in the published data, the block is simply missing — so it waits for a daytime ops window.

```bash
cd ~/streetscape-tracker
# 1. Dry run: lists the runs it would update and counts the rest. Reads one per-run JSON per run, never a census.
.venv-makelab2/bin/python scripts/recompute_run_stats.py \
    --data-dir /projects/makeabilitylab/streetscape-tracker/data \
    --provider mapillary --regenerate-json-mapillary-meta
# 2. Splice the block into those runs' per-run JSON, then rebuild the aggregate.
.venv-makelab2/bin/python scripts/recompute_run_stats.py \
    --data-dir /projects/makeabilitylab/streetscape-tracker/data \
    --provider mapillary --regenerate-json-mapillary-meta --execute >> logs/backfill_321.log 2>&1
# 3. Publish.
.venv-makelab2/bin/python -m streetscape_metadata_tracker.scheduler \
    --config config/scheduler.makelab1.toml regenerate-aggregate --publish
```

It is a separate mode of `recompute_run_stats.py`: it runs no stats pass.
**It selects from each run's JSON alone**: a run whose `mapillary_meta` has a non-null `median_quality_score` (so at least one pano is scored) and no `quality` block.
Everything else stays **absent, never zero**, and the report counts each reason separately: every pre-2026-07-24 run (no `mapillary_meta` at all; 129 of the latest runs, and more across the history the pass scans), every run with no pano row, and every run with the `quality_score` column but no scored pano.
That is what makes **a second pass select nothing**: a selection read off the CSV header re-chose the column-but-unscored runs on every pass, since a rebuild can never give them a block.
**A selected run's block is spliced into its existing JSON**: only the four columns the block needs (`mapillary_quality.BLOCK_COLUMNS`) are read from the census, no other key of the JSON is rewritten, and no catalog row is written.
So a backfilled run's summary is its old summary plus one block, never a re-derivation under today's other definitions (which would mix two JSON forms inside one city's series).
The one exception is a run whose JSON is missing or unreadable: the CSV header decides, and a run whose CSV carries `quality_score` is rebuilt whole through `regenerate_run_json`, which also sets `runs.json_filename`; the report lists these separately.
**Even four columns of a Mapillary census are up to millions of rows per run**, so on production this is a daytime job and **never inside a night**: `--execute` is refused while a `run-due` is in flight (`scheduler._run_due_in_flight`, the check `recompute_run_diffs.py` makes), but like that script it checks only when it starts, and the nightly batch never checks for it.
If step 2 might not finish before 02:00, disable the timer for the night (`systemctl --user disable --now streetscape-tracker.timer`, and `enable --now` afterwards; never `stop`, which the #369 watchdog re-arms).
Only this mode carries the in-flight gate; the stats pass keeps its existing behaviour.
It is not combinable with `--only` or `--regenerate-json` (exit 2): it returns before either would run.

## Backfilling the capture-history summaries (issue #109)

#109 gives each GSV capture-history harvest (#2) a sibling summary JSON, which the aggregate points at and `city.html` renders ([`capture-dates.md`](capture-dates.md)).
`scripts/harvest_gsv_history.py` writes it from now on; a harvest cataloged before the deploy has only its CSV, and the aggregate reads summaries and never builds them, so that city shows no capture-history section until this backfill runs.
**The dry run is also how to learn whether production holds any harvest at all** — none may exist, in which case it reports `0 rows` and there is nothing more to do.

```bash
cd ~/streetscape-tracker
# 1. Dry run: one line per history_harvests row (would write / up to date / MISSING CSV), writes nothing.
.venv-makelab2/bin/python scripts/backfill_history_json.py \
    --data-dir /projects/makeabilitylab/streetscape-tracker/data
# 2. Write the missing summaries.
.venv-makelab2/bin/python scripts/backfill_history_json.py \
    --data-dir /projects/makeabilitylab/streetscape-tracker/data --execute
# 3. Rebuild the aggregate and publish.
.venv-makelab2/bin/python -m streetscape_metadata_tracker.scheduler \
    --config config/scheduler.makelab1.toml regenerate-aggregate --publish
```

It contacts no endpoint and writes no catalog row: it reads each `history_harvests` row's CSV through `fileutils.load_history_csv_file` and writes the summary atomically beside it.
That is why it needs **no timer pause and no in-flight gate**: a publish running beside it ships the old file or the new one, never half of one.
An existing summary is left alone unless `--force` (the way to re-derive every summary after the guard's definition moves); `--city` limits it to one city.
A row whose CSV is missing from `--data-dir` is reported and skipped, and the script then exits 1.

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

## Registering a purposive manifest on production (`panoramax_360_cities.csv`, issue #406)

**Added after the 2026-08-22 split.**

The exact sequence for landing a vetted manifest of new cities, written for the 40-city `panoramax_360_cities.csv` and valid for any manifest in its format.
The selection, the vetting table and every query override are in [`worldwide_sampling.md`](worldwide_sampling.md) ("`panoramax_360_cities.csv`"); this section is only what to run on makelab2, in order, and why.
Every path below is production's `[paths]` from `config/scheduler.makelab1.toml`; run from the production checkout.

**1. Deploy `main` first.**
When this runbook was written (2026-10-04, branching from `9da8911`) production ran `f1885f5`, 53 commits behind that `main`, with the catalog at schema v17 against v19; `main` has moved since, so compare the production checkout's `git log -1` with `origin/main` on the day rather than trusting either count.
The commits it needs carry PR #411's fill, PR #408's Panoramax stage-1 raise (60/min, 8,000/day per channel — the budget the pricing below assumes) and the manifest itself; `enable-city` (#374) is already on production.
Deploy per `deploy/README.md` and let the catalog migrate on connect before registering anything, so the new rows are written by the code that will collect them.

**2. Register, disabled, then audit the boundaries.**

```bash
python scripts/register_frame.py --manifest panoramax_360_cities.csv \
    --db-path /projects/makeabilitylab/streetscape-tracker/data/streetscape_tracker.db \
    --notes-label "panoramax 360 programmes" --overlap-km 5 --max-center-km 10
# read the dry run: expect newly-registered=40 (each row marked -> NEW), reused-existing=0, already-registered=0; then the same with --execute
```

- `--overlap-km 5`, not the default 25, because the default silently ALIASES a genuine neighbour onto an existing city instead of registering it (PR #298's lesson: Johns Creek and Sandusky were 16–17 km from cities already registered).
  The manifest's own test already holds every row more than 25 km from every other row and every city registered since the screen, so a nonzero `reused-existing` in the dry run means the catalog holds a city this record does not know about; stop and find it.
- `--max-center-km 10` because the vetting split cleanly there: every row's geocode landed within 7.5 km of its GeoNames point.
  `--center-from-geonames` is deliberately NOT passed — a row that geocodes differently on the day should be skipped and listed for review, not quietly recentered.
- `--notes-label` is what makes the batch selectable later: without it all 40 claim to be worldwide-frame cities in `cities.notes`.
- Each `--execute` line prints the frozen W x H; compare it with the vetting table and stop on any difference — Nominatim can answer differently than it did on 2026-10-04.
  (The boundary audit below may later resize a city on purpose; that difference is expected, and it makes that city's price stale.)

Cities register with `enabled = 0`, so nothing is collected yet.
Then run the boundary-audit chain (`audit_city_boundaries.py`, then `docs/worldwide_sampling.md` step 2's four steps) over this batch only, in its own audit directory so the catalog-wide report is not overwritten.
The batch is selected by its notes label, so no id can be dropped by hand:

```bash
DATA=/projects/makeabilitylab/streetscape-tracker/data
CITY_ARGS=()
while read -r id; do CITY_ARGS+=(--city "$id"); done < <(python -c '
import sqlite3, sys
conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
for (cid,) in conn.execute("SELECT city_id FROM cities WHERE notes LIKE ? ORDER BY city_id", ("panoramax 360 programmes (geonameid %",)):
    print(cid)
' "$DATA/streetscape_tracker.db")
echo "${#CITY_ARGS[@]}"   # expect 80: 40 ids, two words each
python scripts/audit_city_boundaries.py --data-dir "$DATA" \
    --cache audit/pmx406/nominatim_boundary_cache.jsonl --report audit/pmx406/boundary_audit_report.csv \
    "${CITY_ARGS[@]}"
# 1. dry run, then read the plan: "Re-register" is the auto-resize count, "Deferred" the manual-review count
python scripts/reregister_boundaries.py --data-dir "$DATA" \
    --report audit/pmx406/boundary_audit_report.csv --out-dir audit/pmx406
# 2. apply the auto-resizes
python scripts/reregister_boundaries.py --data-dir "$DATA" \
    --report audit/pmx406/boundary_audit_report.csv --out-dir audit/pmx406 --execute
# 3. render the review page: expect manual=<Deferred>, resize=<Re-register> and "Resize cities skipped (unchanged geometry): 0"
python scripts/build_boundary_review.py --data-dir "$DATA" --audit-dir audit/pmx406
# 4. apply the human decisions for the DEFER cities: dry run, then --execute
python scripts/apply_decisions.py --data-dir "$DATA" --decisions <the exported boundary_decisions.csv>
```

The four steps are #91's sequence, and their order is load-bearing:

1. **Dry run, then read the plan.**
   It writes `reregister_plan.csv` (small `UNDER` cities, OSM bbox ≤ 30 km on both axes) and `manual_review.csv` (every other non-`OK` verdict); `build_boundary_review.py` renders only the cities on those two lists, so without this step the page is empty and the gate passes having checked nothing.
2. **`--execute`.**
   This is safe here because every city in the batch is disabled and has no runs, so a recenter-and-grow resets no diff continuity.
   It is also required: `build_boundary_review.py` shows a plan city only once its geometry has CHANGED from the audit snapshot, so after a dry run alone every auto-resize city is silently dropped, counted only in a `Resize cities skipped (unchanged geometry)` line and a "not shown" banner — and the page is not empty whenever any DEFER city exists, so nothing else would catch it.
3. **`build_boundary_review.py`.**
   Resized cities show before and after, and DEFER cities show the OSM boundary beside the frozen grid; a nonzero `Resize cities skipped (unchanged geometry)` means step 2 did not run.
   A city with an `OK` verdict appears on neither list, so an empty page is a pass only when the report's `verdict` column says every city is `OK`.
4. **`apply_decisions.py` for the DEFER cities.**
   A resize that looks wrong on the page is undone the same way, with its "Revert to grid before resize" decision.

**A resized city's prices are stale.**
The vetting table in `worldwide_sampling.md` and the tranche table in step 4 below were priced on the registered geometry, and the auto-resize grows and recenters a grid while `apply_decisions.py` can set any size; re-price any city either one changed (its new W x H is in `reregister_plan.csv` or the decisions export) before enabling its tranche — including whether it still clears KartaView's 1,000-request enrol ceiling — and move it to a later night if it no longer fits.
The batch stays selectable afterwards: `update_city_geometry` APPENDS its audit note to `cities.notes`, so the notes-label query above still returns all 40.

The audit geocodes one structured query per city through Nominatim (about 40 requests), never a provider.

**3. Enable by tranche, one tranche per night: `scheduler enable-city CITY`.**
`enable-city` enrols the opt-in pairs BEFORE flipping `enabled` (#374), so the city's grid runs and walks pair on one UTC date: Panoramax only on a nonzero one-city screen (a few z6 `grid` tiles each, sent at enable time to the Panoramax host), KartaView only at an estimate ≤ 1,000 — which all 40 clear (the largest is Norman's 691), so all 40 are enrolled on KartaView unless `enroll-city CITY --channel kartaview --remove` and `--channel kartaview_streets --remove` are set first (an explicit membership is never overwritten).
An enabled city is due on every default channel (gsv, gsv_streets, mapillary, mapillary_streets) the next night, and a never-collected city ranks FIRST in each channel's queue (`NULLS FIRST`), so a tranche is what that night collects before anything else; that is why the batch is staged rather than enabled at once.
Enable after the night's batch has finished and before the next 02:00 run, preview each with `--dry-run`, and pass the production config:

```bash
python -m streetscape_metadata_tracker.scheduler enable-city lons-le-saunier--bourgogne--france \
    --config config/scheduler.makelab1.toml --dry-run
```

The tranche's OSM networks must be frozen during the day, so the gsv_streets walks never contact Overpass at night (#341).
`streetscape-prefreeze.timer` already does this daily at 15:00 (up to 30 min late) with `--nights 2 --limit 40 --pause-s 120 --execute --alert`, so a tranche enabled BEFORE the timer fires needs nothing more.
Only a tranche enabled after that day's pass needs a manual one, and it keeps the timer's `--limit 40`, because without it the plan covers every cold network in the window and Overpass volume is uncapped.
Do not overlap the timer's pass: both take the Overpass host lock, so one of the two exits busy (80).
The script is a dry run unless given `--execute`, so read the listing first and then freeze:

```bash
python scripts/prefreeze_street_networks.py --config config/scheduler.makelab1.toml --nights 1 --limit 40             # lists only
python scripts/prefreeze_street_networks.py --config config/scheduler.makelab1.toml --nights 1 --limit 40 --execute   # freezes
```

Before the next tranche, read the night's log: every tranche city collected (or paused at a cap, exit 83, which resumes), no Mapillary block (exit 75), no Panoramax 403/429 (a refusal reverts #405's stage), no Overpass latch.

**4. Pricing and the enable order.**
From the vetting table: the 40 grids hold 13,682,485 GSV grid points (≈ 4.75 h at the 48,000/min nightly pace) plus ≈ 2.71 M GSV walk samples by the scheduler's area proxy (an over-estimate, ≈ 0.94 h), 2,350 Mapillary z14 tiles, 5,385 KartaView requests (≈ 5.6 h at 16/min) and 8,390 Panoramax z15 tiles (≈ 2.3 h at 60/min).
Enabled at once, the Panoramax total alone exceeds the 8,000/day channel budget and the Mapillary total exceeds the 2,260-tile clean combined-night ceiling (`fill_host_ceilings`) before the night's own due Mapillary demand, so the batch is staged.
The tranches below are cheapest-GSV-first (the order #301's 2026-09-10 comment used), each capped at 4 M GSV points and 800 Mapillary tiles so a tranche leaves most of the night's Mapillary room to the regular due queue:

| Night | Cities | GSV points (h at 48k/min) | Mapillary tiles | KartaView requests (h at 16/min) | Panoramax tiles (min at 60/min) |
|---|---|---|---|---|---|
| 1 | Lons-le-Saunier, Kilkenny, Mayenne, Grenoble, Angouleme, Morlaix, Immokalee, Caen, Newton, Bayonne, Douarnenez, Laval | 1,255,892 (0.44) | 276 | 551 (0.6) | 911 (15) |
| 2 | Beaune, Tours, Orleans, Lille, Owatonna, Fort Dodge, Le Havre, Saint-Nazaire, Bordeaux, Marshalltown, Ottumwa, Montpellier | 2,567,532 (0.89) | 482 | 1,003 (1.0) | 1,665 (28) |
| 3 | Lyon, Brest, Mason City, Besancon, Cherbourg, Nantes, Kortrijk, Muscatine, Strasbourg, Montauban | 3,882,789 (1.35) | 703 | 1,552 (1.6) | 2,535 (42) |
| 4 | Toulouse, Mannheim, Ulm, Davenport | 2,558,433 (0.89) | 448 | 1,005 (1.0) | 1,622 (27) |
| 5 | Marseille, Norman | 3,417,839 (1.19) | 441 | 1,274 (1.3) | 1,657 (28) |

So the batch takes **five nights** at the earliest, more if a night is not clean.
The hours are paced request time at the configured rate; the scheduler's own per-city timeout adds ×1.5 headroom plus 600 s, and a tranche shares its night with whatever else is due.
Mapillary, KartaView and Panoramax all checkpoint and resume at a cap (#318, #335), so a tranche that overruns one of those budgets spills into the next night rather than failing; GSV does not (#373 defers a city whose estimated need exceeds the night's remainder), which is the reason the largest GSV grids go last.

## Registering the Mapillary discovery screen's second tranche (`mapillary_discovery_cities_tranche2.csv`, issue #383)

**Added after the 2026-08-22 split.**

The exact sequence for landing the 14-town second tranche on production, in order, with the reason for each step.
The selection, the vetting table and the excluded towns are in [`worldwide_sampling.md`](worldwide_sampling.md) ("`mapillary_discovery_cities_tranche2.csv`").
Every path below is production's `[paths]` from `config/scheduler.makelab1.toml`; run from the production checkout, after the night's batch has finished.

**1. Deploy a `main` that contains the manifest.**
Check `git log -1` in the production checkout first; deploy per `deploy/README.md` and let the catalog migrate on connect before registering, so the rows are written by the code that will collect them.

**2. Register, disabled.**

```bash
DATA=/projects/makeabilitylab/streetscape-tracker/data
python scripts/register_frame.py --manifest mapillary_discovery_cities_tranche2.csv \
    --db-path "$DATA/streetscape_tracker.db" \
    --notes-label "mapillary discovery screen 2026-10-02 tranche 2" --overlap-km 5 --max-center-km 10
# read the dry run: every row "-> NEW", and "already-registered=0 reused-existing=0 newly-registered=14 failed=0"; then the same with --execute
```

- `--overlap-km 5`, not the default 25, because the default silently ALIASES a genuine neighbour onto an existing city instead of registering it (PR #298's lesson).
  The manifest's test already holds every row more than 25 km from every tranche-1 town, from every other row except the Delavan Lake–Como pair (12.7 km apart, grids that cannot overlap), and from every catalog city except two operator exceptions (Fergus Falls 11.5 km from Elizabeth MN, Delavan Lake 19.1 km from Clinton WI, neither grid able to overlap its neighbour's); 5 km admits all of them, so a nonzero `reused-existing` means the catalog holds a city the record does not know about; stop and find it.
- `--max-center-km 10` because every row's geocode landed within 5.5 km of its GeoNames point at vetting.
  `--center-from-geonames` is deliberately NOT passed: a row that geocodes differently on the day should fail and be listed, not be quietly recentered.
- `--notes-label` makes the batch selectable below.
  Tranche 1's label is a PREFIX of this one, so a tranche-1 query written as `notes LIKE 'mapillary discovery screen 2026-10-02%'` now matches both tranches; select tranche 1 with `'mapillary discovery screen 2026-10-02 (%'`.
- Each `--execute` line prints the frozen W x H; compare it with the vetting table and stop on any difference.

**3. Audit the boundaries: the full chain, in its own directory.**
The order is #91's: audit, `reregister_boundaries.py` as a dry run (read the plan), the same with `--execute`, then `build_boundary_review.py`, then `apply_decisions.py` for the cities it deferred.
`build_boundary_review.py` reads the `reregister_plan.csv` and `manual_review.csv` that `reregister_boundaries.py` writes, so a chain that skips that script builds an empty page and the gate passes having checked nothing.
It also DROPS every plan city whose geometry still equals the audit snapshot (`_resize_changed`): after a dry run only, every auto-RESIZE city (verdict UNDER, OSM bbox ≤ 30 km) appears only in the "skipped" banner and is never reviewed or decided.
So `--execute` is passed before the page is built — safe here, because these cities are disabled and have no runs — and the page then shows each resized city before and after, for a spot check; the DEFER cities (`manual_review.csv`) are what `apply_decisions.py` decides.
The ids come from the notes label, so the audit cannot drop one:

```bash
IDS=$(python -c "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); print(' '.join(r[0] for r in c.execute(\"select city_id from cities where notes like 'mapillary discovery screen 2026-10-02 tranche 2%' order by city_id\")))" "$DATA/streetscape_tracker.db")
echo $IDS | wc -w   # 14
python scripts/audit_city_boundaries.py --data-dir "$DATA" \
    --cache audit/mdt2/nominatim_boundary_cache.jsonl --report audit/mdt2/boundary_audit_report.csv \
    $(for c in $IDS; do printf -- '--city %s ' "$c"; done)
python scripts/reregister_boundaries.py --report audit/mdt2/boundary_audit_report.csv \
    --data-dir "$DATA" --out-dir audit/mdt2    # DRY RUN: writes the plan and manual-review CSVs; read the plan
python scripts/reregister_boundaries.py --report audit/mdt2/boundary_audit_report.csv \
    --data-dir "$DATA" --out-dir audit/mdt2 --execute   # applies the RESIZE rows; DEFER rows are untouched
python scripts/build_boundary_review.py --data-dir "$DATA" --audit-dir audit/mdt2
# spot-check the resized cities' before/after in audit/mdt2/boundary_review.html, decide the deferred ones,
# export boundary_decisions.csv into audit/mdt2, then:
python scripts/apply_decisions.py --data-dir "$DATA" --decisions audit/mdt2/boundary_decisions.csv   # dry run, then --execute
```

The audit sends one structured Nominatim query per city (14), never a provider request.
If `reregister_boundaries.py` flags nothing, the review page is empty because every city's verdict is `OK`; read the report's `verdict` column for all 14 to confirm that, rather than reading an empty page as a pass.
A city the audit resizes (or `apply_decisions.py` changes) no longer has its vetted grid, so its row in the vetting table and the step-6 night table is stale: re-price it with `scheduler.estimate_requests` on the new geometry before planning its night.

**4. Enable, one tranche per night: `scheduler enable-city CITY`.**
`enable-city` enrols the opt-in pairs BEFORE flipping `enabled` (#374), so a city's grid runs and walks pair on one UTC date: KartaView at an estimate ≤ 1,000 (all 14 clear it; Reno is the largest at 923), Panoramax only on a nonzero one-city screen.
An enabled city is due on every default channel the next night and a never-collected city ranks first in each queue (`NULLS FIRST`), so the tranche is what that night collects first.
Preview with `--dry-run`, then enable (night 1's twelve towns shown; night 2 is `toms-river--new-jersey--united-states reno--nevada--united-states`):

```bash
NIGHT1="phoenixville--pennsylvania--united-states atwater--california--united-states
  buffalo--minnesota--united-states woodland--california--united-states payson--utah--united-states
  galesburg--illinois--united-states perris--california--united-states
  keene--new-hampshire--united-states tracy--california--united-states
  fergus-falls--minnesota--united-states delavan-lake--wisconsin--united-states
  como--wisconsin--united-states"
for c in $NIGHT1; do python -m streetscape_metadata_tracker.scheduler enable-city "$c" --config config/scheduler.makelab1.toml --dry-run; done
for c in $NIGHT1; do python -m streetscape_metadata_tracker.scheduler enable-city "$c" --config config/scheduler.makelab1.toml; done
```

**5. Freeze the tranche's street networks before its first night.**
After enabling, so the new cities are in tomorrow's slate (#341: a walk on a frozen network never contacts Overpass, and a first walk that does can be stranded by a mid-night refusal):

```bash
python scripts/prefreeze_street_networks.py --config config/scheduler.makelab1.toml --nights 1 --limit 40             # list
python scripts/prefreeze_street_networks.py --config config/scheduler.makelab1.toml --nights 1 --limit 40 --execute   # freeze
```

Without `--execute` the script only lists.
The daily 15:00 timer runs the same pass as `--nights 2 --limit 40 --pause-s 120 --execute --alert`, so a tranche enabled before it runs is frozen by it, and the manual commands are needed only when enabling after the 15:00 timer has run; `--limit 40` keeps them to the timer's one-night ceiling.
Do not run them on an afternoon that already carried a drain or a daytime walk catch-up (Overpass's ~100 queries/day guidance, `provider-access.md`).

**6. Pricing, and the nights.**
From the vetting table, one collection of all 14 is 5,659,858 GSV grid points (1.97 h at 48,000/min) plus 1,123,036 GSV walk samples by the area proxy (an over-estimate, 0.39 h), **811 Mapillary z14 tiles**, 2,221 KartaView requests (2.31 h at 16/min) and at most 2,934 Panoramax z15 tiles (49 min at 60/min, only if the screen enrols the pair).
The Mapillary walks are free on a paired night (#290).
Mapillary is priced against the **2,260-tile clean combined night** (`fill_host_ceilings`, the highest combined night recorded without a block), which also has to hold that night's regularly due Mapillary cities; block 4 (2026-09-28) followed ~4,600 tiles in 24.5 h.
So the tranche is split in two, cheapest GSV first and Reno alone with Toms River:

| Night | Cities | GSV points (h at 48k/min) | Mapillary tiles | KartaView requests (h at 16/min) | Panoramax tiles (min at 60/min) |
|---|---|---|---|---|---|
| 1 | Como, Phoenixville, Delavan Lake, Atwater, Buffalo, Woodland, Payson, Fergus Falls, Galesburg, Perris, Keene, Tracy | 2,565,534 (0.89) | 423 | 1,064 (1.11) | 1,438 (24) |
| 2 | Toms River, Reno | 3,094,324 (1.07) | 388 | 1,157 (1.21) | 1,496 (25) |

Before night 2, read night 1's ledger: its combined Mapillary spend plus 388 must stay under 2,260, and the log must show every tranche city collected (or paused at a cap, exit 83, which resumes), no Mapillary block (exit 75) and no Overpass latch.
Never enable this tranche on the same night as another batch's tranche (e.g. #406's Panoramax manifest): the Mapillary tiles add up against the same per-IP ceiling.
Reno is the owner's decision point ([`worldwide_sampling.md`](worldwide_sampling.md)): leaving it disabled after registration costs nothing.

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

## Checking Mapillary candidates before registering one: `scripts/mapillary_candidate_probe.py` (#406)

**Added after the 2026-08-22 split.**

#406's acceptance item 4: before any Mapillary-only candidate is registered, re-run the Graph API probe the 2026-10-01 research pass abandoned after two requests (its second asked for `limit=2000` and drew HTTP 500 "Please reduce the amount of data you're asking for"; [`experiments/panoramax-world-screen.md`](experiments/panoramax-world-screen.md), finding 6).
The probe sends ONE request per candidate — `graph.mapillary.com/images` over a 2 x 2 km box at the candidate's point, `fields=id,captured_at,creator_id,is_pano`, `limit` at most 200 — at least 3 s apart, and stops at the first answer that is not a 200, retrying nothing.

```bash
python scripts/mapillary_candidate_probe.py candidates.csv                                  # the plan: no request, no token
python scripts/mapillary_candidate_probe.py candidates.csv --execute --out probe-2026-10.csv  # writes probe-2026-10.csv.requests.jsonl too
```

The candidates file needs `name`, `lat` and `lon` columns; the 42 unverified rows live in the research pass's gitignored `experiments/candidate-360-cities-2026-10-01/`, on the laptop that ran it.
Each answered candidate gets its image count, a `capped` flag when the answer filled the limit (the counts are then a FLOOR), the pano count and share, the number of creators and the dominant one's id and share — the single-creator town sweep #406 found at Laurens, Iowa is the shape to look for — and the oldest and newest capture dates.
It reuses `mapillary_user_activity.py`'s client (redirects not followed, the token in a header) and its hard-floor pacer, touches only `graph.mapillary.com` (never the per-IP-blocked tile CDN), and exits 75 on a 3xx or an HTML page, how Mapillary presents a per-IP block — stop then, and do not re-run from that IP for hours.
Run it from a laptop: it refuses a `makelab*` host unless `--allow-collection-host`, and nothing in the scheduler calls it.
It had not been run when it was committed; the first run's request log and result belong beside a writeup in `docs/experiments/`.
