# Publish duration: what should the rsync's timeout be?

**Ran:** 2026-08-20 ·
**Verdict:** a healthy publish is **3.4–25.5 s** (p50 12.1). `PUBLISH_TIMEOUT_S = 600 s` —
~23× the observed max, and the largest value that still fits inside the wind-down window.

## The question

`scheduler._publish` shells out to `sync_data_to_server.sh` and, until
[#230](https://github.com/jonfroehlich/streetscape-tracker/issues/230), was the only child in
the scheduler with no `timeout=`. Every other one is bounded — the collection child by a
per-city timeout, the catalog backup by `catalog_backup.BACKUP_TIMEOUT_S`, the Overpass fetch
by `_deadline()`. rsync has exactly the property those bounds exist for: a half-open SSH
connection, or a stalled NFS mount on the `--local` path, sits indefinitely rather than
erroring.

Bounding it needs a number, and #230 asked for that number to come from observation rather
than from a guess. Two constraints shape it:

- **From below**, it must be far enough above a real publish that a healthy night never trips
  it. The publish is the *last* thing a night does, so a false positive loses a publish whose
  data is already collected — [#167](https://github.com/jonfroehlich/streetscape-tracker/issues/167)'s
  failure shape reached by another route.
- **From above**, it must fit inside the tail. `systemctl stop` is a wind-down that still runs
  the tail ([#206](https://github.com/jonfroehlich/streetscape-tracker/issues/206)), and the
  unit's `TimeoutStopSec` is 30 min.

## Method

Read the scheduler's own rotating logs. No collection, no API calls — this is an analysis over
data already on disk, which is the smallest an experiment gets and is still worth writing down.

**The log holds three populations that look alike, and pooling any two is the whole hazard.**

| population | what it is | usable as a duration? |
|---|---|---|
| `exact` | the `Published in N.N s` line | yes — the real measurement |
| `bound` | `Publishing via …` → the timestamp of the **next** log line | **upper bound only** |
| `failed` | `Publish script failed (…) after N.N s` | no — not a publish duration |

`exact` was added by [#229](https://github.com/jonfroehlich/streetscape-tracker/pull/229) and
is **not yet on prod** (HEAD `df0e1f0` predates it), so **every figure below is from `bound`**.
That interval also contains whatever ran between the publish returning and the next thing
logged — on the nights that have a successor at all, the alert's SMTP send — so the true
durations are somewhat *shorter* than what is quoted here. That direction is the safe one for
sizing a timeout.

`failed` is the sharpest trap, and it fires. Three of the observed failures took **0.05–0.30 s**
(a `--local`/SSH mode mismatch, an immediate rsync exit), and pre-#229 the failure line carried
no elapsed at all — so it is simply the next line after `Publishing via`, 0.05 s later. Left
unrecognised, the bound fallback scores it as a *healthy publish faster than any real one*,
becoming the new minimum of the distribution the timeout is sized from. The parser matches it
explicitly and files it under `failed` with a null duration: counted, never timed.

**And a fourth thing that is not an observation at all.** A healthy pre-#229 night logged
*nothing* after `Publishing via` — that is
[#218](https://github.com/jonfroehlich/streetscape-tracker/issues/218)'s complaint restated —
so where a successor line exists it usually belongs to a *later invocation* hours away. Those
are recognised (a run-start or per-city line, or a delta over the cutoff) and reported as an
**excluded count**, never dropped silently: 34 of the 53 `Publishing via` lines in the archive (16 measurable, 3 failures)
are unmeasurable, and a frame that discarded them quietly would read as coverage it never had.

## Findings

Healthy publishes, **n = 16** nights, 2026-07-21 → 2026-08-20, all upper bounds:

| | min | p25 | **p50** | p75 | p90 | p95 | **max** |
|---|---|---|---|---|---|---|---|
| **all** (n=16) | 3.4 | 6.0 | **12.1** | 14.5 | 20.3 | 24.3 | **25.5** |
| ssh (n=14) | 3.4 | 5.8 | 12.1 | 14.1 | 16.2 | 19.8 | 25.5 |
| local (n=2) | 10.3 | — | — | — | — | — | 23.9 |

Also in the archive but **not** in that distribution: 3 untimed failures, and 34 excluded
`Publishing via` lines (14 where the log simply ends, 20 whose successor is a later run).

The tree walk alone — `sync_data_to_server.sh --local --dry-run`, which builds the full file
list and transfers nothing — measures **2.303 s** as-found and **0.138 s**
on an immediate repeat — three passes ran 2.303 / 0.139 / 0.138 s over **7416** candidate files
(makelab2, 2026-08-20). It is an NFS `stat()` sweep, so it is dominated by the client's dentry
cache, and quoting a single figure would silently pick a side of a 17× spread. Either end of it
is a small fraction of a 12 s publish.

**So the clock buys transfer, not scanning.** What the publish actually moves is
**7409 files / 30.75 GB** of `*.csv.gz` and `*.json.gz`, of which a night ships the handful it
just collected plus the regenerated indexes. The bound therefore has to grow with published
*volume* over time, not with file count.

## Decision

`PUBLISH_TIMEOUT_S = 600.0` in `streetscape_metadata_tracker/scheduler.py`.

- **~23× the observed max** (25.5 s), so a healthy night cannot trip it, with room for the
  published volume to keep growing.
- **Deliberately the same number as `BACKUP_TIMEOUT_S`**, so the tail's two bounded terms read
  alike.
- **It is close to the ceiling, and the ceiling is arithmetic, not taste.** During a wind-down
  the whole tail must fit inside `TimeoutStopSec` = 1800 s, and the other two large terms are
  `BACKUP_TIMEOUT_S` (600 s) plus the measured aggregate + manifest rebuild
  (`_MEASURED_TAIL_AGGREGATE_S`, 435 s on the 19-city night of 2026-08-18). 600 + 435 + 600 =
  **1635 s**, leaving 165 s. Anything above ~765 s no longer fits, and a publish bound that is
  itself SIGKILLed before it can report is the pre-#230 behaviour under a different name.
  `test_stop_timeout_covers_the_publish_tail_it_waits_for` pins the sum from both sides.

Note the sum is a worst case whose terms do not co-occur: a 600 s backup means a `SQLITE_BUSY`
source, and 435 s is the largest aggregate rebuild we have ever measured.

A `TimeoutExpired` is reported as an ordinary publish failure — logged, alerted, nonzero —
never raised, per #167's rule that the tail reports rather than propagates.

## Caveats

- **Every figure is an upper bound** and comes from `bound`, not `exact`. Re-run this after
  #229 reaches prod; the `exact` population will be shorter and n will grow much faster,
  because every night contributes one rather than only the nights that alerted.
- **The sample is biased toward nights that alerted.** Pre-#229 a successor line exists only
  when something else was logged after the publish, which in practice means a failed
  collection. Nothing suggests that changes how long an rsync takes, but it is not a random
  sample of nights.
- **14 of 16 observations are the pre-#215 SSH transport**, which prod no longer uses. The two
  `--local` NFS observations (10.3 s, 23.9 s) sit inside the SSH range, so there is no evident
  regime change — at n=2.
- **A cold publish is the one case 600 s may not cover.** Into an empty docroot the transfer is
  the full 30.75 GB, which needs ≳ 41 MB/s to finish inside the bound. That is survivable
  rather than fatal: rsync's progress is monotone across attempts (completed files persist;
  only the in-flight temp file is lost), so successive nights converge while each one alerts —
  and the operator's actual move for a docroot rebuild is to run the script by hand, outside
  the scheduler, where no bound applies.
- **The bound cannot stop an uninterruptible child.** `subprocess.run` answers `TimeoutExpired`
  with `kill()` and then an *unbounded* `wait()`, so a `--local` child blocked in an
  uninterruptible NFS RPC outlives it — as it would outlive systemd's own SIGKILL. Nothing in
  userspace fixes that one. The failure line prints the bound and the real elapsed as two
  numbers (`timed out at 600 s … after 742.3 s`) precisely so that deferral is visible
  somewhere.

## Replicating

```bash
# On the collecting host (the logs stay there; they are not published or synced).
python scripts/publish_duration_analyze.py --logs-dir logs --time-tree-walk \
    --docs-dir docs/experiments
```

`--time-tree-walk` needs the docroot, so it is a makelab2-only flag; it is read-only (rsync
`--dry-run`) and stamps the host it ran on. Committed record:
[`publish-duration_metrics.json`](publish-duration_metrics.json), which carries the ~20 raw
observations as well as the percentiles, so every number above is traceable to a line in a log.
Sampling invariants are pinned by `tests/test_publish_duration.py`.

## Open

- Re-measure once `exact` exists on prod, and re-size if the real distribution differs from
  these upper bounds by more than the margin assumes.
- The publish is timed but the *aggregate rebuild* still is not — `_MEASURED_TAIL_AGGREGATE_S`
  comes from reading log timestamps by hand. It is the last unbounded term in the sum above.
