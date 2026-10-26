# Running the GSV capture-history backfill on its own host (issue #442)

The `#442` harvester (`scripts/harvest_gsv_history.py`) queries an undocumented, IP-identified Google endpoint for the full capture history at every road-walk sample point.
`docs/capture-dates.md` fixes where it may run, how fast, and when it stops; this file is how that is deployed.
**This directory names no host.**
The host is a small virtual machine with a public address of its own that no lab service shares, no inbound access, no lab credential and no catalog;
which machine, where, and how an operator reaches it are written in the lab's private planning notes, never in this repository (the review of PR #446 scrubbed the pilot writeup of exactly that).
Nothing in `deploy/systemd` applies here: those are the catalog host's user units, and this is a system unit on a machine that exists for this one job.

## Where things live on the host

| What | Path | Notes |
|---|---|---|
| Checkout and its venv | `/srv/streetscape-tracker` | Owned by the `crawler` user; read-only to the unit. No `.env`: the harvester takes no credential |
| Runner | `deploy/crawler/run_gsv_history_queue.sh` in the checkout | What the unit runs, so a `git checkout` moves it with the harvester |
| Work lists | `/srv/crawler/worklists/*.worklist.csv.gz` | Built on the catalog host by `scripts/build_gsv_history_worklist.py` and copied in |
| Queue files | `/srv/crawler/worklists/queue-pass1.txt`, `queue-pass2.txt` | One work-list path per line, in crawl order |
| Results | `/srv/crawler/results/<stem>/` | One out-dir per work list: `<stem>.pass<N>.{raw.jsonl,history.csv,queries.csv}.gz` and `pass<N>.log` (the harvester's output) |
| Latch and lock | `/srv/crawler/results/blocked.json`, `crawl.lock` | ONE per host, shared by every work list and both passes (`--latch-dir` is the results root) |
| Queue log | `/srv/crawler/queue-pass<N>.log` | `START`/`END ... rc=N` per work list and how the queue ended |

## One-time setup

As root on the host:

```bash
useradd --system --create-home --shell /bin/bash crawler
install -d -o crawler -g crawler /srv/crawler /srv/crawler/worklists /srv/crawler/results
sudo -u crawler git clone https://github.com/jonfroehlich/streetscape-tracker.git /srv/streetscape-tracker
sudo -u crawler python3 -m venv /srv/streetscape-tracker/.venv          # 3.11+
sudo -u crawler /srv/streetscape-tracker/.venv/bin/pip install -r /srv/streetscape-tracker/requirements.lock
cp /srv/streetscape-tracker/deploy/crawler/gsv-history-queue@.service /etc/systemd/system/
systemctl daemon-reload
```

Then copy the work lists in (as `crawler`, under `/srv/crawler/worklists/`) and write `queue-pass1.txt`: one absolute path per line, in the order to crawl.
The runner refuses the whole queue before its first request if any listed file is missing.

```bash
systemctl enable --now gsv-history-queue@1       # pass 1; reboot-safe from here on
```

## Operating it

**Status.**
`systemctl status gsv-history-queue@1` says whether the queue is running; the queue log says which work list it is on; that work list's `pass1.log` ends with the harvester's progress line (`N/M sample (P%) after T min`).
`ls /srv/crawler/results/blocked.json` answers "is the host latched?".
There is no alerting yet: a latched or stopped queue is found by looking, so look daily.

**How the queue ends.**
The runner exits with the harvester's own codes, and the unit restarts on none of them but a crash:

| Exit | Meaning | What to do |
|---|---|---|
| 0 | Every work list's pass is complete | Enable the next pass (below) |
| 83 | Some work lists still have rows that never answered | Read which from the queue log; `systemctl start gsv-history-queue@1` re-runs the queue, costing only a ledger read per finished list. If the same lists come back 83 again, read their `pass1.log` and ledgers (`unanswered`) before a third run |
| 86 | The endpoint refused (429/403, an HTML page, the breaker) and the 24 h latch is in force | Read `blocked.json` and the last `pass1.log`. Wait out the cool-down, then `systemctl start`; the harvester refuses to start while the latch holds. `--clear-latch` is a deliberate operator override, not the default |
| 88 | Another crawl holds the lock | Two instances enabled at once; disable one |
| 2 | Our own refusal: argv, a missing or malformed work list, an out-dir written under another column set | Fix the queue or use a fresh out-dir; never append to a pilot-era directory |
| other | A crash | The unit re-runs the queue after `RestartSec` (every crawl resumes from its ledger), three times a day at most; then `systemctl reset-failed gsv-history-queue@1` after reading the journal |

**Stopping and moving code.**
`systemctl stop gsv-history-queue@1` sends SIGINT, so the harvester's final flush writes a complete gzip member; a stop is never a loss.
To move the checkout, stop the unit, `sudo -u crawler git -C /srv/streetscape-tracker checkout <sha>`, dry-run the current work list by hand as `crawler` (`--dry-run` runs the out-dir shape check and prints how many rows are already answered), then start the unit again.
A finished work list is skipped on the re-run at the cost of reading its ledger, so a restart from the top of the queue is cheap.

**Pass 2.**
Only after pass 1's queue has ended 0 (or its 83 stragglers are judged done): `systemctl disable --now gsv-history-queue@1`, write `queue-pass2.txt`, then `systemctl enable --now gsv-history-queue@2`.
Both passes share the results root, the latch and the lock; a pass-2 instance started beside pass 1 exits 88 and does not restart.
Pass 2 is about eight times pass 1's rows (`docs/experiments/gsv-history-sizing.md`), so size the disk for it first:
a results directory holds the raw bodies verbatim, and Seattle measured 3.1 KB per answered pass-1 sample and 2.2 KB per pass-2 sample across the three files (2026-10-26).

**Getting the results back.**
The crawler is catalog-free, so its results reach the catalog host by copy, into gitignored `experiments/`, never `data/`.
The raw store is the irreplaceable copy (the endpoint may outgrow the parser; `scripts/reparse_gsv_history_raw.py` rebuilds the parsed files from it), so copy `results/` whole and keep it on backed-up lab storage.
Landing them in the catalog is the importer, a follow-up to `#442`.

## What the tests pin

`tests/test_deploy_crawler.py` runs the runner for real against a stub interpreter (the exit-code table above, queue order, the argv it builds, the pre-flight check), reads the exits the unit must not restart on out of the harvester's own constants, checks that the unit runs the checkout's runner as `crawler` under the sandbox the file declares, and refuses any host identity (a cloud provider, region, instance type or id, address) in this directory and the harvester's docs.
