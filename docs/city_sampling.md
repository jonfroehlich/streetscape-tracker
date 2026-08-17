# City sampling & catalog provenance

This document is the **authoritative record of which cities GSV Tracker follows
and how each one entered the catalog.** It exists so the tracked set is not
misdescribed (it is *not* "just US state capitals") and so the sampling
methodology is citable in publications.

The catalog (`data/streetscape_tracker.db`, table `cities`) is the source of truth for
what is tracked; this doc explains *how that set was assembled*.

## Current composition (snapshot: 2026-07-08 — counts reconciled, see [#112](https://github.com/jonfroehlich/gsv-tracker/issues/112))

Numbers below are from the live catalog and will drift as collection continues —
regenerate them with the queries in [Reproducing these numbers](#reproducing-these-numbers).

- **1,143 cities registered** — **1,099 US** and **44 non-US** across ~30
  countries (Canada 8; Costa Rica 3; then 1–2 each: Taiwan, Spain, Portugal, New
  Zealand, Mexico, Kenya, India, Chile, Brazil, Vietnam, Turkey, Switzerland,
  and more).
- **~1,132 cities carry pano data**; 11 are registered but empty.
- Runs are **overwhelmingly archival baseline imports** (1,154 baseline vs. 4
  live non-baseline runs at this snapshot). Baseline vintages by `run_date`:
  2023 (13), 2024 (83), **2025 (1,025)**, 2026 (33).

> **⚠️ Caveat — this data was NOT collected by v2; v2 has not launched.** The
> ~1,132 cities carry **imported archival baseline** runs (stream 2 below) —
> scrapes collected mostly Dec 2024–Feb 2025 by the earlier `gsv-capture-dates`
> effort and imported as `is_baseline=1` during the July 2026 migration. The v2
> live temporal-tracking pipeline has produced only **4 runs, ever** (Adrian OR
> and Corvallis OR test cities). "v2 has not launched" and "the catalog holds
> ~1,132 cities of data" are both true at once. Do not read these counts as v2
> collection.

> **Count reconciliation ([#112](https://github.com/jonfroehlich/gsv-tracker/issues/112), resolved).**
> The catalog figures — **1,143 registered / 1,132 with pano data** — are
> canonical. The earlier working estimate of **"~488 cities"** does **not**
> correspond to anything in the catalog and is a **stale pre-import / working
> estimate** (likely a pre-migration snapshot or a since-superseded published
> subset); it is not a real project count and there is no data discrepancy to
> reconcile. Always prefer explicit definitions (`registered` vs `has archival
> baseline` vs `collected live under v2` vs `published to web`) over a single
> "cities tracked" number. Note also
> that catalog `created_at` is *not* a history: the migration rebuilt every row
> in July 2026, so all rows share a `2026-07` timestamp.

## How a city enters the catalog — four streams

### 1. US census-stratified selection

A per-state stratified sample of US cities spanning the full population range,
not just large cities. Tooling and inputs live in [`../cities/`](../cities/)
(see [`cities/README.md`](../cities/README.md) for the full method).

- **Source:** US Census Bureau Subcounty Resident Population Estimates, Vintage
  2023 (`cities/SUB-IP-EST2023-POP.xlsx`).
- **Method:** for each state, take the **capital** + the **largest city**, then
  **randomly sample 5 cities from each of 4 population quartiles**; skip states
  with < 22 qualifying cities.
- **Output:** `cities/selected_cities.txt` (886 cities).
- **Caveat:** the quartile sampling is **not seeded**, so the checked-in file —
  not a re-run — is the frozen study list.

### 2. Archival baseline imports (issue #93)

Historical GSV scrapes from **2023–2024** (collected by the separate
`gsv-capture-dates` effort) were imported as **`is_baseline=1`** runs so the
temporal series has a real starting point rather than beginning empty. This is
the **dominant** source of catalog rows today. Baseline runs are never renamed
and keep stable published URLs. See issue #93 and
[`CLAUDE.md`](../CLAUDE.md) ("Legacy pre-2026 data files").

### 3. Manual / ad-hoc additions

Cities added by request over time — the primary reason the catalog spans ~30
countries despite the US-focused sampling tool. Two common triggers:

- Direct requests from collaborators or users.
- **Project Sidewalk** deployments: a prospective Sidewalk partner city is
  evaluated for GSV data quality, so candidate cities get added and collected to
  assess coverage/recency before committing.

Any city added ad-hoc via `python streetscape_tracker.py "City, Region, Country"`
(or a line in `cities.txt`) geocodes once, freezes its grid geometry, and
registers — becoming a permanent tracked city.

**For a deployment inquiry, use `scheduler assess-city` instead** (issue #215):

```bash
# Boundary fit + per-channel cost, no provider requests
python -m streetscape_metadata_tracker.scheduler assess-city "Newport, Kentucky" --estimate
# Then collect: both road walks + the cheap Mapillary grid run, publish, print the numbers
python -m streetscape_metadata_tracker.scheduler assess-city "Newport, Kentucky" --yes
```

It answers from **street coverage** (share of road-km with imagery), which is
what a deployment decision turns on, rather than grid coverage — the two diverge
badly, because grid points land on river, rail, parkland and rooftops. Highland
Heights measured 55.6% grid vs **92.8% of street-km**; Covington 8.2% vs
**50.8%** on Mapillary. It also reports what share of the sampled rectangle is
actually inside the city boundary before anything is spent: for the four NKY
counties that was only 49–69%, with the remainder largely Cincinnati.

The expensive GSV **grid** run is deliberately not part of it — a newly
registered city is enabled and immediately due, so it leads the next nightly
batch's queue on its own.

### 4. Worldwide stratified frame (issue #110)

A deterministic, reproducible worldwide sample
(`continent × size-band × GSV-coverage-regime`, ~56 cities) built from vendored
GeoNames data in `data_sources/`. Methodology is fully specified in
[`worldwide_sampling.md`](worldwide_sampling.md); registration is
`scripts/register_frame.py` (idempotent, dry-run by default, reuses
already-registered overlapping cities via aliases, registers new cities
`enabled = 0` until boundary-vetted). Frame cities enter the scheduler only
after the boundary-audit workflow accepts their grids (tracked in
**issue #110**).

## Relationship between the streams

- Streams 1–3 are **already in the catalog**; stream 4 registers via
  `scripts/register_frame.py` and stays out of the scheduler until vetted.
- Streams are **additive and non-conflicting**: grid geometry is frozen per
  city, so a city registered by any stream keeps its geometry, run history, and
  published URLs regardless of how later cities are added.
- Coverage *rates* are cross-provider comparable (grid-point coverage), but raw
  pano counts are census-vs-sample across providers — see
  [`CLAUDE.md`](../CLAUDE.md).

## Reproducing these numbers

```bash
# total registered / US vs non-US
sqlite3 data/streetscape_tracker.db \
  "SELECT COUNT(*) FROM cities;"
sqlite3 data/streetscape_tracker.db \
  "SELECT CASE WHEN country_code='US' THEN 'US' ELSE 'non-US' END, COUNT(*) \
   FROM cities GROUP BY 1;"

# cities with real data vs registered-but-empty
sqlite3 data/streetscape_tracker.db \
  "SELECT COUNT(DISTINCT city_id) FROM runs WHERE unique_panos > 0 OR status_ok > 0;"

# baseline vs live runs, and baseline vintages
sqlite3 data/streetscape_tracker.db \
  "SELECT is_baseline, COUNT(*) FROM runs GROUP BY is_baseline;"
sqlite3 data/streetscape_tracker.db \
  "SELECT substr(run_date,1,4), COUNT(*) FROM runs WHERE is_baseline=1 GROUP BY 1 ORDER BY 1;"
```
