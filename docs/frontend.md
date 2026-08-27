# Web frontend, and what it consumes

The static site and the published contracts it reads. Read before touching `www/` or the aggregate
and per-run JSON that feed it.

Split out of `CLAUDE.md` (2026-08-22); the router keeps this topic's short rules and points here for the evidence and detail.
An edit that changes a rule belongs in both files; anything written since the split is under its own heading and says so.

## Web frontend (`www/`)

**Web frontend (`www/`).** Static vanilla JS + Leaflet + Chart.js 4, no build step.
`streetscape-utils.js` has the `PROVIDERS` registry (labels, the short `shortLabel` column-header form and the `panoCountingModel` sample-vs-census token the pivoted tables' headers read, per-provider color-scale anchors — GSV 2007 vs Mapillary 2014
— viewer deep-links, attribution) and `adaptCityRecord(rec, provider)` which flattens v1/v2/v3 aggregate records and emits normalized `pano_count`/`pano_age_stats`/`capture_year_histogram` keys;
`index.js` is the overview map with a GSV/Mapillary radio toggle (persisted as `?provider=`, re-renders without refetching);
`city.js` streams the run's csv.gz (provider derived from the filename token; GSV rows filtered to official `© Google`, Mapillary rows all kept) and has a snapshot `<select>` filtered to the active provider's runs.
Data is fetched from `https://makeabilitylab.cs.washington.edu/public/streetscape-tracker/data/`, populated by `sync_data_to_server.sh` (which publishes only `*.csv.gz`/`*.json.gz` — logs, the DB, and bare CSVs are excluded).
Mapillary attribution is required by their ToS and rendered in the Leaflet attribution control.
`grid.html`/`streets.html`/`driving.html` are **configuration over a shared chassis**, not bespoke pages: `table-utils.js` + `table-controls.js` (plus `histogram-slider.js`, loaded only by the two pivoted pages) provide sorting (nulls sink in both directions),
diacritic-folded search, select/range/histogram-range/boolean filters, grouped two-row headers, column presets + picker, full URL round-trip and a clickable distribution strip, so a page is a column-descriptor array, a row model and a fetch.
Two constraints that shape what a new page may do: there is **no pagination or virtualization**
— every keystroke re-renders all matching rows via `innerHTML`, and the largest page is `driving.html` at ~3,900 rows (`grid.html` fell from 1,501 to ~1,190 when it pivoted to one row per city)
— and `createTableControls` **owns the whole query string**, so two instances on one page would fight over it (which is why `driving.html` renders unmatched plan areas as a summary section rather than a second table).

## The two data-table pages are pivoted: one row per city (issue #250)

*Written after the split.*

**The two data-table pages are pivoted: one row per CITY, providers as sub-columns (issue #250).**
They used to be one row per (city, provider), which defeated their own headline question — sorting by any metric scattered a city's series to opposite ends of the table, so "does Mapillary beat GSV here?"
could not be read off the screen at all, and grid.html's "Multiple providers" checkbox existed only to *find* comparable cities because the layout could not *show* the comparison.
Pivoted, the two numbers sit side by side under one grouped header and a signed **Δ** column answers it directly.
Seven things are load-bearing.
**(1) The Δ pair is FIXED (`mapillary − gsv`), not "best − GSV"** — best's identity changes from row to row, so that column's sign would mean something different in every one — and it is **null unless BOTH operands are present**, since treating a missing operand as zero turns "this city has no Mapillary run" into "Mapillary is 51 points behind" and then *sorts* it as one.
Two groups deliberately get no Δ at all: per-provider **pano counts** are census-vs-sample and their difference answers nothing, and streets' **walk-to-walk change** is each provider against ITS OWN previous walk, so "GSV improved 4 points and Mapillary improved 1" is two facts about two series rather than one difference.
A third provider gets its own sub-columns automatically (everything fans out from `PROVIDERS`), and gets no Δ until someone widens the bare `deltaPct`/`deltaPctAny`/`deltaMedianAge` row keys that `?sort=` and the `dcov` filter name.
**(2) The city set is the UNION across providers**, never the intersection: `adaptCitiesPayload` drops a city with no runs for the provider it is adapting for, so intersecting would hide every single-provider city — which is most of them.
Frozen-grid geometry collapses to ONE column rather than repeating per provider, because it is a city property and is precisely what makes the providers' coverage rates comparable; first non-null wins, since a provider's pre-v3 record carries nulls that must not overwrite a real value.
**(3) Providers fold into columns; NETWORK TYPES do not.**
Both providers walk the same deterministic sample points on the same frozen network, so their numbers belong side by side — but `drive` and `all_public` divide by different street-km totals, so streets.html keeps the network as a page-level `<select>` (one network at a time) and `rowKey` = `${city_id}|${network_type}` becomes the table's tie key, `city_id` no longer being unique.
That select declares `defaultValue: "drive"`, which is a real chassis behaviour and not a cosmetic one: **absence of the param means the default**, an unknown value falls back to it rather than to unset (dropping the filter would double every city's rows on a hand-edited URL), serialization omits it at default, no blank "any" option is offered, and "Clear all" resets *to* it.
**(4) A per-city row needs a per-provider way in, and EVERY per-provider cell is one.**
`city.html` derives its provider from the run filename, so the City cell can only ever open one series.
`providerColumnGroup` therefore takes a `linkFor` and wraps each leaf cell's content in a whole-cell `<a class="provider-cell-link">` — which is why a per-provider `cellFor` returns `{html, className?, title?}` (the cell's INNER parts) rather than an assembled `<td>`, and why `coverageCellParts` exists beside `coverageCellHtml`.
The Δ leaf is never linked: it belongs to no one provider.
A cell whose provider has no run here is left plain rather than linking nowhere, and the cell's own `title` beats the link's where it has one (the walk-to-walk churn behind a Δ is saying more than "opens this series").
The link inherits the cell's colour and only underlines on hover/focus — a pivoted row carries six to twelve of them and a table of blue numbers is unreadable.
**Known cost, accepted:** this roughly doubles the table's tab stops (grid.html ~3,500 → ~8,300 at 1,187 rows); tabbing a table of that size was already impractical and AT navigates tables by cell rather than by tab, so the trade is a way into every provider's data against a keyboard path nobody uses.
On streets those filenames come **only** from the `${provider}|${city_id}` aggregate entry, never from the bare-`city_id` NAME fallback — that fallback exists so a city walked by a provider it has no grid run for still gets a label, and following it would open a different provider's series.
**(5) streets.html's default sort is the GSV coverage leaf, not `pctBest`**, which preserves the page's historical coverage-desc opening; `pctBest` is a filter field with no column of its own, and ordering by an invisible column is exactly what `createSortableTable`'s drop-the-sorted-column fallback exists to prevent.
The provider asymmetry is deliberate and commented.
**(6) Old links degrade rather than break**: `?provider=gsv` still selects the same cities (the value vocabulary only *gained* a `multi` option, which absorbed the old checkbox), `?both=1` is silently ignored by the unknown-key parser, and a pre-pivot `?sort=pct` falls through `setSortTo`'s unknown-key guard to the page default.
**(7) The REGISTRY is not the payload: every leaf fans out over the providers actually COLLECTED, not over `Object.keys(PROVIDERS)`.**
The registry is what the site knows how to render and it is a strictly larger set than what has been published — KartaView is registered (#225/#251) and, since #248, a scheduler channel whose membership is **opt-in**, so it publishes only for the cities an operator enrolled and the 2026-08-22 aggregate carries 1,187 GSV cities, 1,067 Mapillary and **zero** KartaView.
Fanning out over the registry took `grid.html` from 20 columns to 26 and its default preset from 9 visible to 12 (streets 23 → 32, 9 → 12), every KartaView leaf an em-dash
— and worse, offered “Collected by → KartaView”, which matches no rows AND, because that select is also the numeric **scope** (below), redirects every slider onto an all-null field whose empty domain then falls back to the descriptor's `min`/`max`, i.e.
an arbitrary 0–1 axis on the age filter.
So `pivotGridRows` reports which providers its payload contained and `walkProvidersIn` which ones the manifest walked, and the columns, the presets, the Δ pair and the scope options are all built from that — the same distinction `GRID_DELTA_PAIRS` already drew for the Δ leaves, widened from “is it registered” to “is it here”.
Two halves of the contract are easy to break in opposite directions: the module-level `GRID_COLUMNS`/`STREET_COLUMNS` stay the **full-registry** build, so the `?sort=`/`?cols=` vocabulary does not depend on tonight's data, while a `?provider=` naming an uncollected provider is simply absent from `options` and `parseTableState` drops a value no option offers,
so such a link degrades to unscoped rather than to a dead scope.
And this is a layout fact rather than a tidiness one: the default view carries three grouped metrics, so each additional collected provider is three more ~90px leaves against the same 1500 − 280px measure the presets are sized to.
**(8) A grouped leaf's header button carries `pickerLabel` as its `aria-label`.**
The visible leaf label is a bare provider name repeated under every metric group, so grid's default preset exposes eight sort buttons under **three** distinct accessible names, in one tab order and one rotor list.
Reading the *table* is fine — AT associates the `scope="colgroup"` cell with the body cells during table navigation — but a controls list gets the button's accessible name and nothing else, and the disambiguating text lived only in a hover-only `title`.
The column picker's flat checkbox list hit the identical problem one layer over, and `pickerLabel` is the string it already computes for it.
Emitted only where a descriptor supplies one, so driving.html's header markup does not move.

## Histogram-slider filters replaced the distribution strip (issue #250)

*Written after the split.*

**Those two pages replaced the sorted-column distribution strip with per-filter histogram-sliders; driving.html keeps the strip, byte-identically.**
The strip visualizes the ACTIVE SORT COLUMN over the CURRENTLY FILTERED rows, which made it change its own meaning twice over: re-sorting silently swapped its metric, and clicking a bar filtered the rows the strip was drawn from, so the picture collapsed under the very interaction it invited.
`histogram-slider.js` gives each numeric filter one histogram, on one metric, with a dual-handle brush
— the interaction mechanics (two native range inputs on one track, thumbs clamped against each other, the band between them draggable as a window, the z-index hack that keeps the low thumb grabbable when both are pinned at the top) lifted from index.js's legend slider and generalized from integer bucket indices to continuous values.
Three of its properties are the reason it is not just prettier.
**The bars are computed over `rowsExceptFilter`** — every OTHER control's selection, never its own — because feeding a slider its own output makes the bars vanish under the brush that drew them, and dragging back out cannot restore bars that are no longer there.
**The axis is fixed**, seeded from `allRows` (clamped by the descriptor's declared min/max) and never recomputed under a brush, so brushing shrinks the bars and never moves the handles' meaning out from under the reader — a change of provider scope is the one thing that re-seeds it, for the reason in the next paragraph.
**And `setDomain` snaps that axis outward to whole steps**: a max that is not a whole number of steps above min is *unreachable*, because the browser snaps a range input's value down to the last valid one
— so `hi` rested just below the top of the data, full span never read as "no filter", and, worse, the highest-valued rows silently dropped out of the table the moment the OTHER handle moved (a 0–85.1 axis at step 1 pins `hi` to 85, quietly excluding the 85.1% row).
Steps come from `sliderStepFor` (~100 arrow presses across the domain, on a 1/2/5 ladder, never 0 and never `step="any"`, which would put float noise like `18.442000000000004` in the URL).
The min/max **number inputs stay** as the precision path and keep their `data-filter`/`data-bound` hooks verbatim — `syncControlsToState`, `handleControlChange` and the e2e selectors all read them, and the range handles deliberately carry no `data-filter` so `querySelectorAll('[data-filter=KEY]')` still returns exactly two elements.
A bound typed on the right moves the handles on the left and the component's normalized value is read BACK as the state, so the two halves of one control cannot show different windows.
Cost, measured in-browser against the real published aggregate (1,187 cities, 2,254 series): one filter pass **1.4 ms**, all three crossfilter histogram passes **3.6 ms**, sort + `innerHTML` of 311 matched rows **12.3 ms** — so the extra passes are ~4 ms of a ~17 ms keystroke, comfortably inside a frame.
**What protects driving.html is enforcement, not care**: `theadHtml` emits exactly the pre-#250 single `<tr>` when no visible column carries a `group` (a test compares the two strings), and `controlsHtml`'s default `layout: "inline"` was verified byte-identical against `origin/main` over driving.js's real descriptors
— which is why that branch carries a literal `"\n      "` where the old `${filterControls}` interpolation sat.
Every new CSS rule is scoped under `.with-sidebar` / `.streets-main--wide` / `.th-group` / `.hist-*` / `.delta-*`, none of which driving.html emits.
Two things the first cut of this got wrong, both caught in review.
**The snapped axis is the ONLY axis, and the chassis has to be handed it back.**
`setDomain` snapped internally while `syncHistogramDomains` kept the RAW extent and passed that to `histogramBuckets`, so the bars were bucketed over `[dataMin, dataMax]` while the thumbs and `.hist-fill` were positioned over `[snappedMin, snappedMax]` — both painted across the same 100% width, i.e.
two axes under a comment claiming there was one copy precisely so this could not happen (measured: **1.05%** of the track at the data max on the 0–85.1 coverage axis, 0.30% on the Δ axis, 0.22% on street km).
`setDomain` therefore *returns* the snapped domain, `getDomain()` exposes it, and the chassis stores what it was given rather than what it sent.
**And “a bound typed on the right moves the handles on the left” had no return leg.**
The component's normalized value was read back as the state but never written back into the two number inputs, so `normalizeSliderRange`'s three jobs
— swapping crossed handles, nulling a bound sitting at a domain edge, clamping one beyond it
— were invisible there: typing `90` into the min box while the max box read `10` settled the table, the thumbs and the URL on 10–90 while the boxes still read 90 and 10, which is the same two-halves-disagree shape `writeRangeInputs` was introduced for.
It is now written back on **commit** (blur or Enter) rather than on every debounced keystroke, because a bound half-way to `95` reads as `9` and normalizes to the domain edge or to null — rewriting the box at that moment would wipe the digit about to follow.
One CSS note in the same family: `.hist-slider` takes `touch-action: pan-y`, not `none`.
`none` is right for the window drag but the rule covers a 56px full-width strip including the bars, which are not draggable at all — three of them on grid.html, in a sidebar that below 900px is an ordinary scrolling column — so a touch starting anywhere on a slider could not scroll the panel.
`pan-y` still claims the horizontal gesture, which is all `pointermove` uses: it reads `clientX` only.

## "Collected by" is a scope, not just a row filter (issue #250)

*Written after the split.*

**"Collected by" is a SCOPE, not just a row filter (issue #250 follow-up).**
A pivoted row holds one number per provider, so "coverage over 80%" is not a complete question until it says WHOSE coverage — and the first cut of the sidebar did not compose the two controls at all: the sliders always read a best-across field (`pctBest` = max, `medianAgeBest` = min) while the select only narrowed which cities were LISTED.
Measured on the live catalog, "Mapillary + ≥ 80%" returned **56 cities and not one of them had Mapillary coverage over 80** — every one matched on GSV's number, and since nothing anywhere reaches 80% on Mapillary (catalog max 47.6) the truthful answer was zero rows; the bars had the same defect, drawing GSV's spread under a Mapillary selection.
Picking a provider now points each numeric filter at that provider's column, redraws the bars over its distribution, re-seeds the axis to its range and rewrites the wording that says whose numbers these are (the legend AND both thumbs' `aria-label`, or a screen reader announces "Minimum Grid coverage %" while the handle brushes Mapillary's column);
"Any provider" keeps the exists-semantics with the quantifier spelled out ("any provider reaches", "freshest of any") rather than a bare "best" that never said across what.
The mechanism is a descriptor opt-in — `fieldFor`/`labelFor`/`testFor(values)`, resolved by `resolveFilters` into the live view that `applyFilters`, `rowsExceptFilter` and the histograms all read — and **a descriptor declaring none of them passes through by IDENTITY**, which is what keeps driving.html and the plain `range` filters unaware of any of it.
Three decisions inside it.
**(1) A scope change re-seeds the axis**, the single exception to the fixed-axis rule above: it is a different gesture from brushing, and a Mapillary-scoped coverage axis genuinely should not span GSV's range.
**(2) A scope change CLEARS that filter's window** rather than carrying it across, because clamping silently rewrites the question — "≥ 80%" against a 0–47.6% axis becomes "≥ 47.6%" and returns a row where the honest answer is none.
A URL restore does NOT clear (`clearOnScopeChange: false`): there the field and its window arrived together, and dropping it would discard the shared link's own filter.
**(3) What follows the scope is whatever is incomplete without a whom, and no more.**
The Δ filter does not: a difference is a question about the pair, so there is no single provider's column it could read — nor does streets' street km, a property of the OSM network rather than of anyone's walk of it.
A scoped filter need not be numeric: streets' "Has Δ since last walk" resolves its `test` and not merely its wording, because "walked twice" is as incomplete a question as "coverage over 80%" until it says by whom.
One bug the clear path exposed is worth keeping named: three paths change a window (a typed bound, a dragged handle, a scope clear) and they must all leave the precision inputs agreeing with `state.values`, which is why they now share one `writeRangeInputs` writer — the scope clear was re-filtering the table while the min box went on reading "80".

## The filter sidebar is a native `<details>` (issue #250)

*Written after the split.*

**The filter sidebar is a native `<details>`, and the one hole that leaves is closed in JS.**
grid.html and streets.html put search/selects/columns/sliders/checkboxes in a ~280px column beside the table (page measure 1200 → 1500px) that collapses to a "Filters" disclosure at ≤900px; native semantics give keyboard and AT support for nothing.
Above the breakpoint the `<summary>` is `display: none` and the panel is simply a column — which means a panel collapsed on a narrow screen and then widened would be closed with its only toggle gone, stranding filters that are in the URL and cannot be seen or changed.
`wireSidebarDisclosure` re-opens it on widening, one-way (narrowing never closes what the reader opened).
`controlsHtml`'s `layout: "sidebar"` orders the sections search → selects → columns → numeric windows → booleans → clear, partitioning by filter TYPE with an "everything else" bucket so a type added later renders in the wrong place rather than not at all;
below the breakpoint the same controls become a `repeat(auto-fit, minmax(220px, 1fr))` GRID rather than a wrapping flex row, since a tall wide histogram-slider and a short narrow select interleave into a ragged block under `flex-wrap`.
**`.table-sidebar` itself carries the card chrome and the sticky full-viewport height**, with `.table-controls` transparent inside it
— the obvious alternative, a flex chain stretching the controls to fill, does not survive contact with `<details>`: modern Chromium gives it a `::details-content` box, so `.controls-region` is not a flex item of the disclosure at all and simply does not grow (measured: sidebar 886px, controls 637px).
**And the pages lead with one sentence, not a screen of prose** (`.page-head` / `.page-lead` / a closed `.page-about` disclosure): these two are instruments rather than articles, and the preamble was pushing both the table and its filters below the fold.
driving.html keeps its full `.streets-intro`, which is doing a different job — that page's verdicts do not mean anything until the plan-vs-observed contradiction has been explained.

## Site navigation + street-coverage discoverability

**Site navigation + street-coverage discoverability.** Road-walk coverage was collected and rendered but unreachable from the site root: `index.html` had no chrome at all and only a walked city's own page showed anything.
Three additions, all fed by the existing manifest (no `cities.json.gz` v3→v4 bump — that stays #102).
(1) A shared `.site-header` (44px, `position: fixed`, styles in `streetscape-shared.css`) on all three pages — brand, Map/Streets nav, About→GitHub; it replaced city.html's standalone `#back-link`.
Because the map is full-bleed (`#map { inset: 0 }`) with every panel `position: fixed`, the header **floats** and each panel carries its own 44px-clearing `top` (index.css), with one `.leaflet-top .leaflet-control { margin-top: 54px }` rule for Leaflet's own controls.
(2) `METRICS.streets` — a fourth color-by metric reading `street_coverage_pct_by_length`, which `mergeStreetwalkStats(cities, manifest)` (streetscape-utils.js) joins onto the adapted records by (city_id, provider).
It is **not** a fallback to grid coverage: a different denominator (street-km driven vs. grid points with imagery), so an unwalked city is "No data", rendered at reduced fill (`baseFillOpacity`) with the banner stating "N of M cities walked".
The overview popup's street line shows in **every** metric mode — it is the main discovery surface.
(3) `streets.html`/`streets.js`/`streets.css` — a top-level listing of published road-walks, joining the manifest (keyed by `city_id`, no display name or run filename) against the aggregate to get labels and the `city.html?file=` link.
Deliberately not a second map.
The manifest helpers (`fetchStreetwalkManifest`/`lookupStreetwalk`) moved from `street-coverage.js` into `streetscape-utils.js` since all three pages now need them.

## Grid sample points in the aggregate

**Grid sample points in the aggregate.** `_build_provider_summary` also promotes `total_search_points` and a `grid` block (width/height/step) out of the per-run JSON's `search_grid`, additively within schema v3.
`coverage_rate_percent` is a share **of** those points — `json_summarizer:433` counts distinct `(query_lat, query_lon)` pairs in the run CSV, which is exactly its denominator
— so publishing the rate without it leaves a reader unable to tell a village's 40% from a metro's.
All four keys are **indexed, not `.get()`-guarded**, matching the `search_area_km2` line beside them: they come from one dict literal in `generate_city_metadata_summary_as_json` and have coexisted since the file's earliest tracked form (verified against all 1,171 per-run JSONs on disk, legacy and archival included), so a `search_grid` missing one is a corrupt file worth failing on.
Guarding would also publish `{width: null, height: null, step: null}` — a *truthy* all-null block that no `if (rec.grid)` consumer can reject, the exact failure the absent-not-null convention exists to prevent.
It is the **latest run's** grid, not the city's current frozen geometry: the two diverge for cities resized catalog-only by `scripts/cap_oversized_grids.py` (#166) until their next collection, and pairing the run's denominator with the run's geometry is the correct half — label it as the run's grid in any UI.
`adaptCityRecord` surfaces both normalized (null on v1/v2 records, which will never carry them).
