# Capture-date precision: what a strict reader cost us

**Ran:** 2026-08-21 ·
**Verdict:** 9 of 1,170 run CSVs carry **month**-precision capture dates, and the loader's
strict `'%Y-%m-%d'` turned every date in them into `NaT` while their pano counts stayed
perfect. No file on disk mixes precisions — which is exactly why the fix **pins** the
format rather than inferring it. And the corpus's apparent day precision is a fiction:
**100.00%** of GSV day-shaped dates land on the 1st, because `standardize_capture_date`
pins them there at write time.

## The question

Issue [#226](https://github.com/jonfroehlich/streetscape-tracker/issues/226) starts as a
one-line bug — `fileutils.load_city_csv_file` parsed `capture_date` with
`format="%Y-%m-%d"` — but three things had to be answered with numbers before the fix
could be sized, and none of them is answerable from the code:

1. **What formats are actually on disk?** The fix is only as good as the corpus it was
   chosen against, and the choice between `format="ISO8601"` and a format-free
   `pd.to_datetime` turns entirely on whether any single file mixes precisions.
2. **Does ISO8601 cost anything?** [#157](https://github.com/jonfroehlich/streetscape-tracker/issues/157)
   made census memory and CPU a standing constraint, so "more permissive parse" needs a
   number rather than a shrug.
3. **How big is the repair?** `recompute_run_stats.py --regenerate-json` re-reads every
   rebuilt run's CSV, and #226 widened its trigger to "any moved capture-date column".

All three are read out of data already on disk — no network, no provider credentials, no
new requests, which is the smallest an experiment gets. It is written up anyway, because
all three get re-asked the next time a date definition or a reader moves, and re-deriving
them means re-reading 15 GB.

## Method

`scripts/capture_date_precision_analyze.py`, three independently selectable measurements:

| measurement | what it reads | cost |
|---|---|---|
| `sweep` | the `capture_date` column of every run CSV in `data/` | one column, whole corpus |
| `parse` | a synthetic 3,000,000-row column | seconds |
| `asymmetry` | every catalogued gsv run, recomputed under **both** readers | full corpus, stats twice |

Only files whose name parses as a **run** (`naming.parse_filename`) are swept: diff,
streetwalk and history artifacts share the directory and carry different schemas, and
counting them would inflate the denominator. That is why this says 1,170 and not 1,177,
which is every `*.csv.gz` in `data/`.

The classifier is a **shape** test, not a validity test. `"2022-13"` counts as month
precision even though no calendar contains it: the question is which formats a reader
must accept, and folding the calendar-invalid rows into "other" would understate the
affected population by exactly the rows most worth knowing about.

The asymmetry pass calls the real `calculate_run_stats` — not a re-derivation of its date
path — twice per run, once with `'%Y-%m-%d'` and once with `'ISO8601'`, and compares each
answer to what the catalog stores. It reports a **direction** per run, not a magnitude.

## Findings

### 1. The corpus is day precision plus one legacy pocket of month precision

Over 730,071,871 `capture_date` cells in 1,170 run files:

| shape | cells | share of all | share of *dated* |
|---|---:|---:|---:|
| absent | 561,379,741 | 76.89% | — |
| day (`YYYY-MM-DD`) | 163,762,141 | 22.43% | 97.08% |
| month (`YYYY-MM`) | 4,929,989 | 0.68% | 2.92% |
| year (`YYYY`) | 0 | 0% | 0% |
| anything else | 0 | 0% | 0% |

**Take the denominator seriously**: 76.89% of cells are ZERO_RESULTS fill, so a share
over rows rather than over *dated* rows describes the sampling grid, not the imagery. The
month population is 0.68% of the corpus and 2.92% of its dates.

By file, the split is total rather than partial — every file is one precision or none:

| file population | files |
|---|---:|
| day only | 1,149 |
| month only | **9** |
| no dates at all | 12 |
| **two or more precisions** | **0** |

The nine, with their row counts:

| file | rows | month | absent |
|---|---:|---:|---:|
| `nairobi_kenya_…` | 3,851,400 | 1,074,203 | 2,777,197 |
| `lagos_nigeria_…` | 3,132,900 | 747,097 | 2,385,803 |
| `auckland_nz_…` | 2,534,352 | 973,594 | 1,560,758 |
| `taipei_taiwan_…` | 1,458,736 | 759,013 | 699,723 |
| `la_piedad_mx_…` | 1,297,764 | 117,777 | 1,179,987 |
| `amsterdam_nl_…` | 1,016,094 | 504,925 | 511,169 |
| `saskatoon_sk_…` | 987,800 | 337,864 | 649,936 |
| `nakuru_kenya_…` | 456,740 | 176,529 | 280,211 |
| `zurich_switzerland_…` | 426,790 | 238,987 | 187,803 |

**The sweep's denominator is the corpus; the repair's is the catalog, and they differ by
one.** `saskatoon_sk_…` is not a registered run — it is an orphan superseded by
`saskatoon--sk_…` (double hyphen), which carries day precision — so no catalog audit and
no repair script can see it. Eight runs need repairing; nine files were being misread.

### 2. Nothing on disk mixes precisions — which is an argument *for* pinning the format

Zero files carry two precisions. That is the number that makes a format-free
`pd.to_datetime(errors="coerce")` *appear* to work on today's corpus, and it is exactly
why it is the wrong choice: pandas infers **one** format from the first non-null value and
silently coerces everything at another precision to `NaT`. Measured both ways on pandas
3.0.1:

```
["2022-09-15", "2022-09"] -> [2022-09-15, NaT]
["2022-09", "2022-09-15"] -> [2022-09-01, NaT]
```

A one-way check passes by luck and reports the trap as absent, so the committed metrics
record both orderings. `download_kartaview` already pins `format="ISO8601"` after meeting
this from the other direction — an API that mixes second and millisecond precision inside
one page.

**The corpus is one file away from mixing.** `standardize_capture_date` accepts `YYYY-MM`
and `YYYY` on input and normalizes them; any future writer that skips it, or any archival
import that preserves the provider's own string, produces the mixed file this table says
does not exist yet.

### 3. The corpus's day precision is mostly a fiction, and that reframes the bug

| provider | files | dated cells | day cells landing on the **1st** |
|---|---:|---:|---:|
| gsv | 1,167 | 153,385,687 | **148,455,698 of 148,455,698 — 100.00%** |
| mapillary | 3 | 15,306,443 | 459,611 of 15,306,443 — 3.00% |

Mapillary is the control: 3.00% ≈ 1/30 is what genuine day precision looks like, and its
dates come from epoch milliseconds. GSV's **100.00%** is not a coincidence — `download_gsv`
notes the API returns `%Y-%m` "most commonly", and `standardize_capture_date` pins reduced
precision to the 1st before the row is ever written.

So the nine legacy files are not an exotic minority format. They are the *unnormalized*
form of what all 1,167 GSV files already contain, and the loader's ISO8601 parse resolves
them to precisely the value the modern writer would have stored. That is the strongest
argument that pinning to the 1st is a restoration rather than a guess.

### 4. ISO8601 is not slower

Best of 3 over 3,000,000 day-precision strings:

| parse | seconds |
|---|---:|
| `format="%Y-%m-%d"` | 0.470 |
| `format="ISO8601"` | **0.460** |
| format-free (inferred) | 0.459 |

There is no #157 cost to pay. The permissive parse is, if anything, marginally faster than
the strict one on this corpus's dominant shape.

### 5. The tell: under the strict reader a repair could only ever CLEAR a date

Every gsv run in the catalog (1,157; none missing, none unreadable), recomputed under both
readers and compared to its stored values — one verdict per run, since oldest, newest and
median are a min, a max and a median of one population:

| reader | unchanged | moved | value → NULL | **NULL → value** |
|---|---:|---:|---:|---:|
| `'%Y-%m-%d'` (the old one) | 760 | 383 | 14 | **0** |
| `'ISO8601'` (the fix) | 752 | 383 | 14 | **8** |

**The empty cell is the finding.** Under the old reader a whole-series recompute looked
like it was working — 397 runs changed, every correction in one direction — while the 8
runs that were actually broken stayed NULL, because the tool that exists to repair stats
read through the same reader that could not see them. *A repair handle is only as
permissive as its reader.*

The check that exposes this costs nothing and generalizes to any definition change:
**recompute under the old rule and the new one and compare directions, not magnitudes.** A
pass that can only ever clear a value and never restore one is describing its own blind
spot.

The 14 `value → NULL` runs are correct and are #213's work, not #226's: cities whose
imagery is entirely third-party now honestly report "no Google imagery" instead of a
photosphere's EXIF date.

### 6. The repair is hundreds of runs, not dozens

`recompute_run_stats.py --regenerate-json` rebuilds a run's published JSON when the CSV
holds an impossible date (#213) **or** when the pass moves a capture-date column (#226).
The second trigger's population is the `moved + value→NULL + NULL→value` total above:
**405 of 1,157 gsv runs, 35.0%**, on a catalog that has not yet taken #213's backfill.

Each of those is a second full read of its CSV, on top of the stats pass's own, over a
~15 GB corpus. That is the cost of the repair rather than a defect — those published files
hold pre-#213 numbers and are genuinely stale — but it is hours, not minutes, and it is
why the runbook says `--provider gsv`: without the filter the trigger also reaches
Mapillary census runs, whose CSVs are millions of rows apiece.

## What this decided

- **`format="ISO8601"`, pinned**, not the format-free `to_datetime` the issue proposed
  (findings 2 and 4: inference loses a precision, and pinning costs nothing).
- **Reduced precision resolves to the 1st**, in the loader and in `city.js`'s
  `panoDateOrNull` — finding 3 shows that is what the modern writer already does, so the
  two readers and the writer describe one convention.
- **`--regenerate-json` triggers on a moved date column**, not only on an impossible one
  (finding 5: whether an affected run *also* carries a corrupt date is luck — 3 of the 8
  carry none).
- **The runbook budgets hours and keeps `--provider gsv`** (finding 6).

## Caveats

- **The asymmetry numbers are catalog-state-dependent.** They were taken on a 1,157-run
  gsv catalog that had *not* yet taken #213's backfill, so `moved` is dominated by #213's
  narrowing rather than by #226. Re-run on a backfilled catalog, `moved` collapses and
  `NULL → value` is the only column that still reads 8. The *direction* finding is what
  transfers; the magnitudes are not a constant.
- **The sweep is a shape census, not a validity census.** It does not report how many
  values a parser accepts, only which spellings exist. The parsers' verdicts are the
  asymmetry pass's job.
- **Only `capture_date` is swept.** `query_timestamp` goes through a separate
  `format="ISO8601"` parse in the same loader with no `errors="coerce"` at all, and is not
  measured here.
- **One shape can still raise rather than coerce**: a timezone-aware value beside a naive
  one makes `format="ISO8601"` raise `Mixed timezones detected` *through*
  `errors="coerce"`. Nothing in the repo can write one (finding 3's writers all emit
  `YYYY-MM-DD` or nothing), so it is documented at the loader rather than guarded.
- **No figures.** Two of the three measurements are single counts and the third is a 4×2
  table; a chart would carry less than the tables above.

## Replicating

```bash
python scripts/capture_date_precision_analyze.py --measure all --docs-dir docs/experiments
```

Writes `docs/experiments/capture-date-precision_metrics.json`, the committed record every
number above is taken from; `tests/test_capture_date_precision.py` recomputes them from
it. `--docs-dir` refuses a partial `--measure`, so a subset run cannot overwrite the
committed file with one that is silently missing a measurement.

`sweep` and `parse` need only `data/`; `asymmetry` also needs the catalog
(`data/streetscape_tracker.db`). The whole pass is ~50 minutes of CPU on the 15 GB
corpus, almost all of it in `asymmetry`; `--measure sweep,parse` is about ten.
