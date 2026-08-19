# Deploying the Streetscape Tracker scheduler on makelab1

The scheduler runs as a **user-level systemd timer** on
`makelab1.cs.washington.edu` (a RHEL 9-family host): a oneshot service fires
nightly, collects the cities due that day (staggered quarterly cycle,
bounded by a daily API-request budget), diffs each against its previous
run, regenerates the aggregate JSON, and publishes `data/` to the public
web docroot. All state lives in `data/streetscape_tracker.db`, so crashes and
missed days self-heal.

## Where things live

makelab1 is the **compute** host; storage is NFS from other servers, so
this is deliberately split:

| What | Path | Notes |
|------|------|-------|
| Code + data + DB + logs + `.env` | `/projects/makeabilitylab/streetscape-tracker/` | On the lab fileserver (backed up, group `makelab`). **Not web-served.** Shared with other lab services. |
| Convenience symlink | `~/streetscape-tracker` → the path above | Lets the generic `%h/streetscape-tracker` systemd units and `.env` resolve. |
| Public web docroot | `/cse/web/research/makelab/public/streetscape-tracker/` | On a *different* host (the web-file server); served at `makeabilitylab.cs.washington.edu/public/streetscape-tracker/`. Holds only the flattened website + published `*.csv.gz`/`*.json.gz`. |

Because makelab1 mounts the docroot directly, **publishing is a local
rsync — no SSH to the docroot host**. That is declared as `local = true` in the
config's `[publish]` block, so a hand-run publish behaves like the nightly one;
the systemd unit also still exports `STREETSCAPE_PUBLISH_LOCAL=1` as a
belt-and-braces fallback for older checkouts (issue #215).

## 1. One-time setup

```bash
ssh makelab1.cs.washington.edu

# Clone onto lab storage, and symlink it into home for the systemd units
git clone https://github.com/jonfroehlich/streetscape-tracker.git /projects/makeabilitylab/streetscape-tracker
ln -s /projects/makeabilitylab/streetscape-tracker ~/streetscape-tracker

cd ~/streetscape-tracker
python3.11 -m venv .venv          # 3.11+ for tomllib
# requirements.lock pins exact versions (uv pip compile --universal) so the
# deploy matches CI byte-for-byte; requirements.txt holds the loose floors.
.venv/bin/pip install -r requirements.lock

# API keys — copy your working .env up from the laptop (least error-prone):
#   (from the laptop)  scp .env makelab1.cs.washington.edu:/projects/makeabilitylab/streetscape-tracker/.env
chmod 600 .env                    # seal the keys; the parent dir is group-readable
```

## 2. Move the data + catalog up

The SQLite catalog `streetscape_tracker.db` lives **inside** `data/`, so this
one rsync carries both the ~15 GB of snapshots *and* the catalog itself.
Copying the live catalog is the point: it is the only place the frozen grid
geometry, city aliases, and boundary re-registrations (issue #91) exist —
none of that is reconstructable from the CSV/JSON files alone, and the DB is
never in git. This is a *different* rsync from `sync_data_to_server.sh`, which
publishes to the public docroot and deliberately **excludes** the DB.

First, make sure the catalog is checkpointed and no session has it open, so
rsync copies a consistent file rather than a stale one with pending writes in
the `-wal` sidecar:

```bash
# (on the laptop) flush the WAL into the main .db, then confirm nothing holds it
sqlite3 data/streetscape_tracker.db "PRAGMA wal_checkpoint(TRUNCATE);"
```

Then copy `data/` up (includes the `.db`; the `-wal`/`-shm` sidecars will be
empty after the checkpoint):

```bash
rsync -azh --progress data/ makelab1.cs.washington.edu:/projects/makeabilitylab/streetscape-tracker/data/
```

That's it — makelab1 now has your exact catalog. The migration script below is
a **safety-net no-op** in this path: with the catalog already populated it just
re-confirms every file is registered and reports zero changes. Run it only to
verify (or if you ever seed makelab1 from data files *without* copying the DB —
note that route loses the #91 boundary re-registrations, which live solely in
the catalog):

```bash
cd ~/streetscape-tracker
.venv/bin/python scripts/migrate_to_db.py            # dry run — expect 0 new registrations
.venv/bin/python scripts/migrate_to_db.py --execute  # optional; safe to skip if dry run is clean
```

## 3. Sanity-check the config

The production config `config/scheduler.makelab1.toml` is checked in and
already points at the paths above, enables local publish, and enables email
alerts. Confirm mail delivers, then preview a run:

```bash
echo "streetscape-tracker mail test $(date)" | mail -s "streetscape test" you@example.edu
# NB: --config is global and must come BEFORE the subcommand.
.venv/bin/python -m streetscape_metadata_tracker.scheduler --config config/scheduler.makelab1.toml status
.venv/bin/python -m streetscape_metadata_tracker.scheduler --config config/scheduler.makelab1.toml run-due --dry-run
```

Start conservative for the first few nights — set `max_cities_per_day = 2`
in the TOML, watch, then raise. (See **First full backfill** below.)

## 4. Clean up the public docroot + deploy the website

The docroot currently holds a legacy full-repo checkout (`.git/`, `scripts/`,
`config/`, `.venv/`, `*.py`, …) — none of which belongs on a web server.
`deploy_makelab1.sh` publishes the site **flattened** (so it serves at
`.../public/streetscape-tracker/` with no `/www/` in the URL) and its `--delete`
sweeps that legacy junk, while **protecting** `data/`, `poster/`, `cities/`,
and `data-huge/`. Preview first:

```bash
cd ~/streetscape-tracker
./deploy_makelab1.sh --dry-run     # shows exactly what is added/deleted
./deploy_makelab1.sh               # pulls latest code, then prompts before applying
```

### One-time: rename the public data path (GSV Tracker → Streetscape Tracker)

The public docroot moved from `/public/gsv-tracker/` to
`/public/streetscape-tracker/` (repo/product rename). The frontend
(`STREETSCAPE_DATA_BASE_URL`) and `sync_data_to_server.sh` now target the new
path. On the docroot host, do this **once** so existing published `*.csv.gz`
links (which already point at the old path) don't 404:

```bash
cd /cse/web/research/makelab/public
mv gsv-tracker streetscape-tracker          # move existing data + site in place
ln -s streetscape-tracker gsv-tracker       # old URLs keep resolving via the symlink
```

Run this whenever you push new frontend/backend code. The nightly **data**
publish is separate and automatic (step 5).

## 5. Install the systemd units (user-level, no root)

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/streetscape-tracker.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now streetscape-tracker.timer
loginctl enable-linger $USER       # user services must survive logout
```

The service ships with resource caps (`MemoryMax=8G`, `CPUQuota=400%`,
`Nice=10`, `CPUWeight=50`) so nightly collection can't starve the other lab
services that share the box and the storage array. If `enable-linger` is
disallowed by policy, ask CSE IT to enable lingering for your account.

### Host = makelab2, and the shared-home cutover model

The collection job runs on **makelab2** (its ZFS pool holds `data/` + the
catalog, so compute is data-local — no NFS hop). Two facts drive how the host
is selected:

- `$HOME` (`~/.config/systemd/user/`, `~/streetscape-tracker`, `.env`) is
  **shared NFS across makelab1 and makelab2** — so the unit files, their
  enablement, and `timers.target.wants` are identical on both boxes. Only
  `loginctl` **linger** is per-host.
- The scheduler must run on **exactly one** box (both share one SQLite catalog).

So the active host is pinned two ways: `ConditionHost=makelab2*` in the service
unit (collection is a no-op on any other host) **and** linger only on makelab2:

```bash
# on makelab2 (make it the active host):
loginctl enable-linger jonf
systemctl --user daemon-reload && systemctl --user start streetscape-tracker.timer
# on makelab1 (stand it down; leave the timer enabled — it's the shared symlink):
loginctl disable-linger jonf
systemctl --user daemon-reload
systemd-analyze condition 'ConditionHost=makelab2*'   # verify: fails on makelab1, passes on makelab2
```

The venv is **`.venv-makelab2/`**, a `uv`-managed CPython whose base interpreter
lives on the shared ZFS checkout (`.tooling/`), so it runs on **both** boxes —
unlike a stock `python3.11 -m venv .venv`, whose base `/usr/bin/python3.11`
exists only on makelab1. Provision it with:

```bash
export UV_INSTALL_DIR=$PWD/.tooling/bin UV_PYTHON_INSTALL_DIR=$PWD/.tooling/python UV_CACHE_DIR=$PWD/.tooling/cache
curl -LsSf https://astral.sh/uv/install.sh | sh
.tooling/bin/uv venv .venv-makelab2 --python 3.11
.tooling/bin/uv pip sync --python .venv-makelab2/bin/python requirements.lock
```

To fail back to makelab1: flip `ConditionHost` and move linger the other way.

## 6. Operate

```bash
systemctl --user list-timers streetscape-tracker.timer      # next scheduled run
journalctl --user -u streetscape-tracker.service -f          # live logs
systemctl --user start streetscape-tracker.service           # trigger a run now
.venv/bin/python -m streetscape_metadata_tracker.scheduler --config config/scheduler.makelab1.toml status
```

Rotating file logs also go to `logs/streetscape_scheduler.log`, and dated
catalog backups to `backups/` (see below).

### Stopping a run in flight (issue #206)

```bash
systemctl --user stop streetscape-tracker.service     # graceful wind-down
systemctl --user kill streetscape-tracker.service     # immediate death, no tail
```

**`stop` is a wind-down, not a kill.** The in-flight collection child dies with
the cgroup, the loop declines to start any further channel *or* city, and the
tail still runs — aggregate, streetwalk manifest, driving-plan summary, catalog
backup, publish — so the night's collected runs reach the public site. It exits
**0** and sends no alert. The stopped city's remaining channels are not marked
failed: they keep their cadence, stay due, and lead the next batch's queue.

It is bounded by `TimeoutStopSec=30min`. Reaching that is a SIGKILL with no tail
— the 2026-08-13 shape, where a night's runs sat unpublished until someone ran
`regenerate-aggregate --publish` by hand. If you need the batch dead *now*, use
`kill` and expect to publish by hand afterwards.

Two cautions:

- **Do not type `stop` twice.** The tail runs outside the SIGTERM handler's
  scope, so a second stop during it kills the publish with the default
  disposition. systemd itself only sends SIGTERM once, so one `stop` is safe.
- **A stop mid-city releases the Mapillary and Overpass host locks**, so it is a
  reasonable prelude to the manual work described in the next section.

**The installed unit is a COPY, not a symlink** — editing `deploy/systemd/`
changes nothing about the running service until you copy it over and reload:

```bash
cp deploy/systemd/streetscape-tracker.service ~/.config/systemd/user/
systemctl --user daemon-reload
# Verify the stop timeout is actually live — this is also how the old
# 90-second default was originally confirmed:
systemctl --user show streetscape-tracker.service -p TimeoutStopUSec
#   want: TimeoutStopUSec=30min      NOT: TimeoutStopUSec=1min 30s
```

`daemon-reload` is safe during a live batch; `stop` obviously is not.

### Running anything by hand alongside the scheduler (issue #208)

Mapillary's tile CDN and the Overpass API both rate-limit **per IP**, not per
credential. Our client-side pacing is per *process*, so two collections running
at once on this box present double the configured rate — which is exactly how
makelab2 earned bans from both services in one night on 2026-08-14, from a
detached catch-up script that outlived every config change made to stop it.

A file lock in `locks/` now enforces this. A second process that reaches one of
those hosts **fails immediately** rather than queueing, naming the pid that
holds the lock:

```bash
# Safe to START at any time — neither can double the rate any more. But see
# below: whichever process loses the race gives up, and if that's the batch,
# the city it was on skips that channel tonight. GSV grid collection is
# unaffected either way (Google meters per project, not per IP).
python streetscape_tracker.py "Bend, OR" --provider mapillary
python -m streetscape_street_analyzer.collect "Bend, OR"
```

**What it costs the batch.** The lock is not polite about who wins: whoever
asks second fails. If you start a manual Mapillary run while the scheduler is
mid-city, the scheduler's child is the one that loses, and that city's Mapillary
channel is skipped for the night. The city is **not** marked failed — it stays
due and leads tomorrow's queue — but the night alerts and the unit goes red, so
you'll get an email you caused. That is the intended trade: the alternative is a
skipped collection nobody notices. Prefer running manual work when
`systemctl --user status streetscape-tracker.service` shows the timer idle.

Two rules for manual work:

1. **Use the same lock directory as the scheduler.** The unit sets
   `STREETSCAPE_LOCK_DIR=/projects/makeabilitylab/streetscape-tracker/locks`
   explicitly, because `PrivateTmp=true` and the `%h` symlink would otherwise
   make the two processes derive different paths and never see each other. Both
   the default and the override are `realpath`'d, so `~/streetscape-tracker/locks`
   and `/projects/makeabilitylab/streetscape-tracker/locks` resolve to the same
   lock — but export the unit's value anyway if you are unsure.
2. **A leftover `locks/*.lock` file is not a held lock.** `flock` is released by
   the kernel when the process dies, SIGKILL included. Do not delete lock files
   to "unstick" anything — check `locks/*.lock.owner` for the pid instead.

Exit codes a collection child uses to report a host-level condition. The
blocked/busy split matters: the two have opposite lifetimes, and `run-due`
reacts to them differently.

| code | meaning | what `run-due` does |
|---|---|---|
| `75` | Mapillary's tile CDN refused this host's IP | skips **all** Mapillary channels for the rest of the night |
| `76` | The Overpass API refused this host's IP | skips **all** street channels for the rest of the night |
| `79` | Another local process holds the Mapillary tile lock | skips **only that channel of that city** |
| `80` | Another local process holds the Overpass lock | skips **only that channel of that city** |

A refusal (75/76) is durable — asking again with the next city cannot answer
differently — so the first one trips a night-level breaker. A busy lock (79/80)
ends when the other process does, so escalating it would let a two-minute manual
run cost the batch every Mapillary city of the night.

Both alert unconditionally, exit nonzero, and still publish — and both record
**no** per-city failure, so affected cities stay due and lead the next night's
queue rather than burning their `consecutive_failures` budget (five of those
would quarantine a city for a whole 90-day cycle, and nothing but a success ever
resets that counter).

### On-demand catch-up for one channel (issue #214)

The supported way to catch a channel up — **never** a detached bespoke script,
which is what earned the 2026-08-14 bans:

```bash
python -m streetscape_metadata_tracker.scheduler --config <prod.toml> \
  run-due --provider mapillary --limit 40
```

`--provider` takes enabled channel names (repeatable, or comma-separated) and
`--limit N` overrides `[schedule].max_cities_per_day` for that invocation only
(the nightly unit passes no `--limit` and is unaffected). Both refuse a bad value
with exit **64** rather than running a night that collects nothing: an unknown or
`enabled = false` channel, a value naming no channel at all, or `--limit < 1`.
Exit 64 is deliberately distinct from argparse's own 2.

Routing through `run-due` is the point — it inherits the daily budget ledger,
stalest-first ordering, per-channel cadence and failure counting, the host lock,
fail-fast, the night-level breaker, alerting, orphan salvage and the publish tail.

One real cost: it advances **only** the named channels' clocks, so the cities it
touches stop sharing a run date with their other channels until the cadences
re-converge. The run logs a warning naming the channels left behind.

### Answering a deployment inquiry the same day (issue #215)

A Project Sidewalk inquiry arrives about a city we don't track and the useful
reply happens *today*, not after the next nightly cycle:

```bash
# 1. Price it first: registers the city, reports boundary fit and per-channel
#    cost, issues ZERO provider requests.
python -m streetscape_metadata_tracker.scheduler --config <prod.toml> \
  assess-city "Newport, Kentucky" --estimate

# 2. Then collect + publish + print the numbers.
python -m streetscape_metadata_tracker.scheduler --config <prod.toml> \
  assess-city "Newport, Kentucky" --yes
```

It runs the **GSV road walk**, the **Mapillary road walk** and the cheap
**Mapillary grid run**, regenerates the published JSON, publishes, and prints the
street-km figures plus a city-page link.

**Answer from street coverage, not grid coverage.** Grid points land on river,
rail, parkland and rooftops, so grid percentages understate street availability
badly: Highland Heights measured 55.6% grid vs **92.8% of street-km**, Covington
8.2% vs **50.8%** on Mapillary. The printed report leads with street-km and
labels grid coverage as an area measure that is not the deployment number.

**Read the in-boundary line before spending.** The tracker samples a rectangle.
For the four NKY counties only **49–69%** of each rectangle fell inside the
county, and the remainder was largely Cincinnati — whose dense recent GSV would
have flattered every figure quoted to the partner. Newport, KY scores 46% on
today's geometry. When the fraction is low, consider a compact city grid
alongside the county: pass `--width/--height` **together with `--lat/--lng`**
(size alone is refused, because it would freeze the grid on the OSM bounding-box
midpoint rather than downtown — and geometry is frozen forever).

Notes:

- **Safe to run while the nightly batch is going.** The GSV walk is metered per
  Google *project*, so it carries no per-IP exposure; the Mapillary channels go
  through the host lock and, if the batch holds it, fail fast with exit 79 —
  re-run them later. The command never marks the city failed.
- **It does not run the GSV grid run.** That is the expensive half, and it needs
  no help: a newly registered city is enabled with no successful run yet, so it
  leads the next night's stalest-first queue. The channels it *does* collect
  record a success, so they are not due again for a cycle — the closing report
  says so, along with the paired-snapshot cost that carries (same as
  `run-due --provider`).
- **There is no `--publish` flag, only `--no-publish`.** Publishing follows
  `[publish].enabled`, which is the host's declaration; the override lives on
  `regenerate-aggregate --publish`, the incident-time handle. If publishing is
  switched off in the config, the run prints a NOTE beside the city-page link
  saying the link describes the catalog rather than what is live — read it
  before sending that link to a partner.
- **Publishing needs no environment variable.** `[publish].local = true` in the
  prod config makes `_publish` pass `--local` explicitly. Before that, a
  hand-run publish took the SSH path, failed with rsync code 12, and emailed a
  publish-FAILED alert that looked like an outage — if you see that on an older
  checkout, `export STREETSCAPE_PUBLISH_LOCAL=1` first.

### Backups (verified with CSE IT, 2026-08-05 — issue #145)

What CSE IT confirmed when we asked, after the 2026-07 quota incident (#143)
prompted the question:

- **`/projects/makeabilitylab` (the makelab2 array, NFS-mounted) was not being
  snapshotted or backed up at all** until 2026-08-05, when CSE IT fixed the
  configuration: ZFS snapshots (first one 2026-08-05 12:00), nightly filesystem
  sync to the CSE backup servers, and an off-site sync to UW's **lolo** service
  after that. They also swept *all* makelab data volumes into backups at the
  same time, after finding the setup "woefully incorrectly configured" for the
  second time in two months — so treat "it's on lab storage" as **not** implying
  "it's backed up" for any future deployment, and ask again.
- **The public docroot** `/cse/web/research/makelab` (which serves both our
  published `data/` and the Makeability Lab website's `/media` + `/public`) was
  already on the standard rotation: hourly/weekly/monthly snapshots plus lolo,
  **snapshots retained 1 year**. (The website's *postgres* volume had the same
  torn-snapshot problem the catalog does; its nightly `pg_dump` sidecar shipped
  as `makeabilitylab/makeabilitylabwebsite#1444`, merged 2026-08-07.)
- **SQLite + ZFS caveat:** a snapshot of the *live* WAL-mode catalog may still
  be torn (recent ZFS waits for an in-flight write, but CSE IT wouldn't vouch
  for it). Their recommendation — keep a periodic dumped copy in the same
  directory — is what our own mechanism does, via SQLite's online backup API
  (`conn.backup()`), which is transactionally consistent against a live WAL
  database. **No `VACUUM INTO` is needed.** The ZFS snapshot history of those
  files is what gives us point-in-time recovery beyond our own 14-day window.
  **When restoring, prefer a dated `.backup` file** over a raw copy of
  `streetscape_tracker.db` + sidecars from a snapshot.

#### What the scheduler does (`streetscape_metadata_tracker/catalog_backup.py`)

`run-due` writes `backups/streetscape_tracker.db.{YYYY-MM-DD}.backup` **twice a
night**, and both times matter:

| when | why |
|---|---|
| **before the city loop** (`_backup_catalog_nightly`) | The tail runs after any *loop-level* failure (errored loop, batch deadline, SIGTERM — #167) but **not** after a SIGKILL, which is the documented OOM mode on the Mapillary post-decode path (#157). A tail-only backup is missing on exactly the nights something went badly wrong. Also covers zero-due nights. |
| **in the tail** (`_finish_batch`) | Makes the retained copy reflect the runs, diffs and walks the night actually registered, not the state it started in. Same filename, atomically replaced. |

Properties worth knowing before an incident:

- **Verified before promotion.** Each copy is written to a `.tmp` sibling,
  checked with `PRAGMA integrity_check`, and only then `os.replace()`d into
  place. So a filesystem snapshot can never catch a half-written backup, and a
  bad copy can never overwrite a good one. (This retires the "not literally safe
  at any instant" caveat that applied to the old single rolling file.)
- **14 days of dated copies**, matching the website's `pg_dump` retention. The
  **newest file is never pruned regardless of age** — otherwise a backup failing
  for longer than the window would end with pruning deleting the last good copy.
- **One file per date: the tail copy replaces the pre-flight one.** Deliberate,
  and the one case where it costs you: if something during the night damages the
  catalog *logically* rather than structurally — a bad migration, a write that
  leaves the file valid but wrong — the tail faithfully copies that state over
  the pre-night copy that was fine, and `integrity_check` passes because the file
  is not corrupt, only wrong. The fallback is then yesterday's copy, i.e. one
  night stale. Accepted because the tail copy is the more useful one on every
  ordinary night (it contains the runs the night registered) and the worst case
  is bounded at one night. The alternative — retaining the pre-flight copy under
  its own name, or having the tail decline to promote when the source's row
  counts look worse than what it would overwrite — doubles the footprint or adds
  a heuristic that can refuse a legitimate backup. Revisit if same-day
  point-in-time recovery ever matters.
- **A failed backup is a failed night.** It alerts unconditionally (subject
  `CATALOG BACKUP FAILED`, ignoring `[alerts].failure_threshold`) and exits
  nonzero so systemd shows the unit red. It does *not* withhold publishing — the
  #167 posture: never hide what the night collected. Backups that fail silently
  are the whole reason #145 existed.
- **Bounded, never hung.** `sqlite3`'s backup API retries `SQLITE_BUSY` in an
  unbounded loop (`PRAGMA busy_timeout` does not apply), and the pre-flight copy
  runs *before* the city loop — so a stuck copy would cost the entire night and
  end in a SIGKILL at `TimeoutStartSec`. A progress-callback deadline
  (`BACKUP_TIMEOUT_S`, 10 min) supplies the timeout sqlite3 lacks, and an open
  transaction on the source connection is rejected outright rather than retried.
- **No stray `-wal`/`-shm`.** Each dated copy inherits WAL format from the
  catalog, so *any* read of one — including a read-only one, which cannot clean
  up after itself — leaves a sidecar pair behind. They are cleared at promotion
  and prune time, because nothing binds a WAL to a particular database file:
  left beside a replaced copy, SQLite would replay it into the new one.
- **Provenance.** `backups/backup_status.json` records the last attempt —
  outcome, source DB path, hostname, per-table row counts — written on failure
  as well as success, because a failed backup that wrote nothing is otherwise
  indistinguishable from one that never ran. Row counts exist because a backup
  of a *test-fixture* catalog was once mistaken for the production one.

```bash
# Health of the backups plus an inventory of what exists in only one place
.venv/bin/python -m streetscape_metadata_tracker.scheduler --config config/scheduler.makelab1.toml backup-status
```

Exits nonzero when the newest backup is missing, **older than 48 h**, or the
last attempt failed, so it works as a monitor check. The age gate is not
redundant with the outcome: "the last attempt succeeded" stays true forever once
the scheduler simply stops running — a masked timer, a disabled unit, a
`ConditionHost` that no longer matches after a host cutover — and since the
newest copy is never pruned, that state otherwise looks like one file plus an
`ok` status. Which is #145 again.

#### The out-of-band check (issue #193)

The exit code above is only worth anything if something *runs* it. Until #193
nothing did: the sole caller would have been the scheduler, i.e. the very
process whose absence the staleness gate exists to detect. So the check is
scheduled independently, in its own user timer:

```bash
cp deploy/systemd/streetscape-backup-check.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now streetscape-backup-check.timer
systemctl --user start streetscape-backup-check.service   # run once now
```

- Fires **daily at 12:00 Pacific** (±10 min), deliberately not 02:00: it should
  report on the night that just finished, at an hour when someone is awake. The
  hour is not delicate — the gate is 48 h, so even a 10-hour night still running
  at noon cannot trip it (that night's *pre-flight* copy is hours old).
- `backup-status --alert` emails the report through the same `[alerts]` SMTP
  transport when the verdict is unhealthy, and is **silent when healthy** — a
  daily mail nobody needs is a daily mail nobody reads. The subject names the
  verdict (`NO BACKUPS` / `STALE (N h old)` / `last attempt FAILED`) because on
  a monitor mail the subject is often all that gets read, and those call for
  different first moves.
- `--alert` does **not** change the exit status, so the unit still goes red and
  the command stays usable as a plain check.
- It is **not** wired to `OnFailure=streetscape-tracker-notify@.service`: that
  unit is not installed on makelab2 and `OnFailure=` is commented out in the
  installed collection unit, so building on it would mean building on something
  dormant — and `notify-failure` mails the *scheduler log tail*, where the
  useful body here is the backup report itself.
- Same `ConditionHost=makelab2*` pin as the collection unit, for the same
  shared-NFS-home reason: on the wrong box it would report on a backup directory
  nobody maintains. **A host cutover must flip both.**
  `tests/test_scheduler.py::test_backup_check_unit_matches_the_collection_unit`
  pins that agreement, along with the interpreter and config path.

#### Assets that exist in only one place

Most of `data/` is doubly covered — the project array *and* the docroot's 1-year
rotation. Two things are not, because they are deliberately published nowhere,
and `backup-status` inventories both:

| path | why it is irreplaceable |
|---|---|
| `archive/gsv_driving_plan/` | Google **overwrites** its driving-plan feed in place, so a lost dated snapshot is gone permanently — unlike a run CSV, which can at worst be re-collected at a later date. Kept outside `data/` so the publish rsync can't republish Google's content (#176). |
| `data/osm_cache/` | Frozen OSM networks. Refetching does not restore them: today's OSM yields different edge IDs and sample points, which breaks road-walk diff continuity (#101) rather than merely costing a download. |

#### Restore

```bash
# Verifies the backup, then restores it. Refuses if anything is already there.
.venv/bin/python -m streetscape_metadata_tracker.scheduler --config config/scheduler.makelab1.toml \
    restore-backup backups/streetscape_tracker.db.2026-08-07.backup --to /tmp/recovered.db
```

`--to` defaults to the configured `db_path`. Stop the timer first, and restore
to a scratch path and inspect it before putting it in the catalog's place.

Two refusals, both deliberate — a restore that quietly does something plausible
is worse than one that stops:

- **An existing destination.** Recovering onto a live catalog would destroy the
  thing you are recovering. Move it aside first.
- **Orphaned `-wal`/`-shm` beside the destination.** This is what a real
  incident looks like: the catalog goes bad, you move `streetscape_tracker.db`
  aside, and the sidecars of the process that died stay behind. Nothing binds a
  WAL to a particular database file, so SQLite would replay those frames into
  the restored copy on its first open — handing back the exact state you were
  escaping, with `integrity_check` still saying `ok`. They are **not** deleted
  for you: an orphaned WAL can hold the only copy of the last committed writes,
  so whether it is garbage or your best remaining evidence is your call. Move
  them aside and re-run.

Drilled end to end in `tests/test_catalog_backup.py` — the drill genuinely
deletes a populated catalog plus its `-wal`/`-shm` sidecars and asserts the
restored copy is intact, complete, and still carries its frozen grid geometry
and schema version; a second test leaves the sidecars in place and asserts the
restore refuses rather than silently resurrecting the pre-restore rows.

Recovering files older than our 14-day window goes through CSE IT
(support@cs.washington.edu). **Retention for `/projects/makeabilitylab` beyond
the snapshot/sync/lolo tiers has still not been stated** — ask when it matters.
A restore from CSE IT's tiers (as opposed to our own dated copies) has never
been exercised — budget time for surprises.

**Diagnosing a failed city.** `journalctl` above needs journal read access, which
the service account does not have on makelab2 — so don't rely on it. Three file
logs cover the same ground:

| file | holds |
|---|---|
| `logs/streetscape_scheduler.log` | the scheduler's own decisions, plus the last 25 lines of any failed child |
| `logs/collect_{city_id}_{channel}_{date}.log` | one collection subprocess's **full** output, appended per attempt |
| `logs/streetscape_service_console.log` | anything else the unit emits — uncaught traceback, OOM notice |

A `collection failed` line names the per-attempt log to read. The console log
is a safety net and is **not rotated**; prune it if it ever grows.

### Watching resource use (alongside other co-tenants)

```bash
systemd-cgtop                                        # live CPU/mem per cgroup — streetscape vs co-tenants
systemctl --user show streetscape-tracker.service -p MemoryPeak -p CPUUsageNSec
```

Validate the caps after the first live night: if `MemoryPeak` approaches
`MemoryMax`, raise it (or lower `batch_size`/`connection_limit` in the TOML).
Data lives on NFS, so IO is network (not block-device) — CPU/memory caps and
`Nice` are the real levers; `IOWeight` would not govern it.

### Failure alerts (email)

Enabled in `config/scheduler.makelab1.toml`. Test end-to-end without waiting
for a failure:

```bash
.venv/bin/python -m streetscape_metadata_tracker.scheduler --config config/scheduler.makelab1.toml notify-failure
```

**Transport under the sandbox (issue #144).** `transport = "mail"` delivers via
the local mailer, but that goes through a setgid `postdrop` binary that the
unit's `NoNewPrivileges=yes` blocks — so alerts are *silently lost* from the
hardened unit even though the failure was detected. Use **`transport =
"smtp"`** (now the default in `scheduler.makelab1.toml`): it uses stdlib
`smtplib` to talk to a relay directly, touching no setgid path and no
read-only `$HOME`, so it works unchanged inside the sandbox (and identically
on makelab1 or makelab2).

Confirmed working relay (verified from makelab2, 2026-07-17):

```toml
transport  = "smtp"
smtp_host  = "smtp.cs.washington.edu"   # UW CSE relay, no auth for on-campus hosts, plain port 25
smtp_port  = 25
smtp_from  = "jonf@cs.washington.edu"   # MUST be a real mailbox — see below
```

Gotchas that already bit us: (1) the relay **rejects a non-deliverable
envelope sender** (`550 <streetscape-tracker@makelab2…> Address unknown`), so
`smtp_from` must be a real address — the code's `streetscape-tracker@<hostname>`
default won't work here. (2) `localmail.cs.washington.edu` (the box's own
sendmail smarthost) does **not** resolve for a direct connect; use
`smtp.cs.washington.edu`. For a relay that needs auth, set `smtp_user` +
`smtp_starttls` and keep the password in `$STREETSCAPE_ALERT_SMTP_PASSWORD`
(not the toml). Test end-to-end with the `notify-failure` command above.

**Optional systemd safety net** — for an email even when the process dies
before it can send its own (OOM, kill): install the notify unit and uncomment
`OnFailure=` in `streetscape-tracker.service`:

```bash
cp deploy/systemd/streetscape-tracker-notify@.service ~/.config/systemd/user/
# then uncomment OnFailure= in streetscape-tracker.service and daemon-reload
```

It fires on *any* nonzero exit (run-due returns nonzero on any failed city),
so it can duplicate the scheduler's own threshold email — enable only if you
want belt-and-suspenders coverage.

### First full backfill

Post-#91, every city needs a fresh run on the new frozen geometry, so the
first cycle is a big one-time burst (not steady state). Once a few nights look
healthy, raise `max_cities_per_day` (and optionally trigger extra daytime
batches with `systemctl --user start streetscape-tracker.service`) to catch up, then
drop back to the steady ~quarterly cadence (`max_cities_per_day = 20` keeps
~1,144 cities on the 90-day cycle with headroom).

### Disabling a city

```bash
sqlite3 data/streetscape_tracker.db "UPDATE cities SET enabled = 0 WHERE city_id = '...'"
```

A city that fails `max_consecutive_failures` nights in a row is skipped
automatically until you reset it:

```bash
sqlite3 data/streetscape_tracker.db "UPDATE schedule_state SET consecutive_failures = 0 WHERE city_id = '...'"
```
