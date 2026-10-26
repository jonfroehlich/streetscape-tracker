#!/bin/bash
# The #442 GSV capture-history queue runner (deploy/crawler/README.md).
#
#   run_gsv_history_queue.sh <pass> <queue file>
#
# Walks the queue -- one work-list path per line, in order, blank lines skipped --
# and runs scripts/harvest_gsv_history.py on each, ONE at a time, at the
# harvester's own defaults (60/min, jitter 0.6, one request in flight). The
# harvester, not this script, owns the pacing, the breaker, the per-host latch
# and the crawl lock (docs/capture-dates.md); this script only decides what the
# harvester's exit code means for the REST of the queue:
#
#   0   pass complete for this work list     -> next
#   83  incomplete (rows still failing)      -> next; the queue then ends 83 so a
#                                               re-run revisits it (a finished
#                                               work list costs no requests)
#   86  refused by the endpoint, or latched  -> STOP, exit 86: the latch is the
#                                               address's, so every later work
#                                               list would be refused too
#   88  another crawl holds the host's lock  -> STOP, exit 88
#   2   bad argv, work list or out-dir shape -> STOP, exit 2: deterministic, so a
#                                               re-run would fail the same way
#   *   anything else (a crash)              -> STOP with that code; the unit's
#                                               Restart=on-failure re-runs the
#                                               queue and each crawl resumes
#
# Every work list is checked to exist BEFORE the first request, so a typo on
# line 30 is found now rather than a week in. The harvester's stdin is
# /dev/null: a child that read stdin would eat the rest of the queue.
#
# Layout (the two environment variables relocate it for a test or another host):
#   $CRAWLER_REPO                  the checkout and its .venv  (/srv/streetscape-tracker)
#   $CRAWLER_ROOT/results/<stem>/  one out-dir per work list: the three crawl
#                                  files and pass<N>.log, the harvester's output
#   $CRAWLER_ROOT/results/         the --latch-dir: ONE blocked.json and ONE
#                                  crawl.lock for the whole host, shared by every
#                                  work list and both passes
#   $CRAWLER_ROOT/queue-pass<N>.log  this script's trail: START/END per work list
#                                  with its exit code, and how the queue ended
set -u
if [ $# -ne 2 ]; then
  echo "usage: $0 <pass> <queue file>" >&2
  exit 2
fi
PASS=$1
QUEUE=$2
case "$PASS" in
  1|2) ;;
  *) echo "pass must be 1 or 2, got '$PASS'" >&2; exit 2 ;;
esac
REPO=${CRAWLER_REPO:-/srv/streetscape-tracker}
ROOT=${CRAWLER_ROOT:-/srv/crawler}
RESULTS=$ROOT/results
LOG=$ROOT/queue-pass$PASS.log
PYTHON=$REPO/.venv/bin/python

[ -r "$QUEUE" ] || { echo "$QUEUE: no such queue file" >&2; exit 2; }
[ -x "$PYTHON" ] || { echo "$PYTHON: no interpreter (is CRAWLER_REPO right?)" >&2; exit 2; }
MISSING=0
while IFS= read -r WL; do
  [ -z "$WL" ] && continue
  [ -r "$WL" ] || { echo "$WL: no such work list" >&2; MISSING=$((MISSING + 1)); }
done < "$QUEUE"
if [ "$MISSING" -ne 0 ]; then
  echo "$MISSING work list(s) missing; nothing started" >&2
  exit 2
fi

cd "$REPO" || exit 2
mkdir -p "$RESULTS" || exit 2
stamp() { date -u +%FT%TZ; }
INCOMPLETE=0
while IFS= read -r WL; do
  [ -z "$WL" ] && continue
  STEM=$(basename "$WL" .worklist.csv.gz)
  OUT=$RESULTS/$STEM
  mkdir -p "$OUT" || exit 2
  echo "$(stamp) START pass=$PASS $STEM" >> "$LOG"
  "$PYTHON" scripts/harvest_gsv_history.py --worklist "$WL" --pass "$PASS" \
    --out-dir "$OUT" --latch-dir "$RESULTS" < /dev/null >> "$OUT/pass$PASS.log" 2>&1
  RC=$?
  echo "$(stamp) END   pass=$PASS $STEM rc=$RC" >> "$LOG"
  case $RC in
    0) ;;
    83) INCOMPLETE=$((INCOMPLETE + 1)) ;;
    *) echo "$(stamp) QUEUE STOPPED pass=$PASS rc=$RC ($STEM)" >> "$LOG"; exit "$RC" ;;
  esac
done < "$QUEUE"
echo "$(stamp) QUEUE DONE pass=$PASS incomplete=$INCOMPLETE" >> "$LOG"
[ "$INCOMPLETE" -eq 0 ] || exit 83
exit 0
