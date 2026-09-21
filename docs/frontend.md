# Web frontend, and what it consumes

The static site and the published contracts it reads. Read before touching `www/` or the aggregate
and per-run JSON that feed it.

Split out of `CLAUDE.md` (2026-08-22); the router keeps this topic's short rules and points here for the evidence and detail.
An edit that changes a rule belongs in both files; anything written since the split is under its own heading and says so.

## Web frontend (`www/`)

**Web frontend (`www/`).** Static vanilla JS + Leaflet + Chart.js 4, no build step.
`streetscape-utils.js` has the `PROVIDERS` registry (labels, the short `shortLabel` column-header form and the `panoCountingModel` sample-vs-census token the pivoted tables' headers read, per-provider color-scale anchors — GSV 2007, Mapillary 2014, KartaView 2016, Panoramax 2022
— viewer deep-links, attribution) and `adaptCityRecord(rec, provider)` which flattens v1/v2/v3 aggregate records and emits normalized `pano_count`/`pano_age_stats`/`capture_year_histogram` keys;
`index.js` is the overview map with one radio per registered provider (persisted as `?provider=`, re-renders without refetching);
`city.js` streams the run's csv.gz (provider derived from the filename token; GSV rows filtered to official `© Google`, Mapillary rows all kept) and has a snapshot `<select>` filtered to the active provider's runs.
Data is fetched from `https://makeabilitylab.cs.washington.edu/public/streetscape-tracker/data/`, populated by `sync_data_to_server.sh` (which publishes only `*.csv.gz`/`*.json.gz` — logs, the DB, and bare CSVs are excluded).
Mapillary attribution is required by their ToS and rendered in the Leaflet attribution control.
`grid.html`/`streets.html`/`driving.html` are **configuration over a shared chassis**, not bespoke pages: `table-utils.js` + `table-controls.js` + `histogram-slider.js` provide sorting (nulls sink in both directions),
diacritic-folded search, select/range/histogram-range/boolean filters, grouped two-row headers, column presets + picker, a filter sidebar and full URL round-trip, so a page is a column-descriptor array, a row model and a fetch.
All three load all three scripts and pass the same options; the chassis has no per-page layout switches left.
Two constraints that shape what a new page may do: there is **no pagination or virtualization**
— every keystroke re-renders all matching rows via `innerHTML`, and the largest page is `driving.html` at ~3,800 rows (`grid.html` fell from 1,501 to ~1,190 when it pivoted to one row per city)
— and `createTableControls` **owns the whole query string**, so two instances on one page would fight over it (which is why `driving.html` renders unmatched plan areas as a summary section rather than a second table).

## The two data-table pages are pivoted: one row per city (issue #250)

*Written after the split.*

**The two data-table pages are pivoted: one row per CITY, providers as sub-columns (issue #250).**
They used to be one row per (city, provider), which defeated their own headline question — sorting by any metric scattered a city's series to opposite ends of the table, so "does Mapillary beat GSV here?"
could not be read off the screen at all, and grid.html's "Multiple providers" checkbox existed only to *find* comparable cities because the layout could not *show* the comparison.
Pivoted, the two numbers sit side by side under one grouped header and a signed **Δ** column answers it directly.
Nine things are load-bearing.
**(1) The Δ pair is FIXED (`mapillary − gsv`), not "best − GSV"** — best's identity changes from row to row, so that column's sign would mean something different in every one — and it is **null unless BOTH operands are present**, since treating a missing operand as zero turns "this city has no Mapillary run" into "Mapillary is 51 points behind" and then *sorts* it as one.
Two groups deliberately get no Δ at all: per-provider **pano counts** are census-vs-sample and their difference answers nothing, and streets' **walk-to-walk change** is each provider against ITS OWN previous walk, so "GSV improved 4 points and Mapillary improved 1" is two facts about two series rather than one difference.
A third provider gets its own sub-columns automatically (everything fans out from `PROVIDERS`), and gets no Δ until someone widens the bare `deltaPct`/`deltaPctAny`/`deltaMedianAge` row keys that `?sort=` and the `dcov` filter name.
**(2) The city set is the UNION across providers**, never the intersection: `adaptCitiesPayload` drops a city with no runs for the provider it is adapting for, so intersecting would hide every single-provider city — which is most of them.
Frozen-grid geometry collapses to ONE column rather than repeating per provider, because it is a city property and is precisely what makes the providers' coverage rates comparable; first non-null wins, since a provider's pre-v3 record carries nulls that must not overwrite a real value.
**(3) Providers fold into columns; NETWORK TYPES do not.**
Every provider walks the same deterministic sample points on the same frozen network, so their numbers belong side by side — but `drive` and `all_public` divide by different street-km totals, so streets.html keeps the network as a page-level `<select>` (one network at a time) and `rowKey` = `${city_id}|${network_type}` becomes the table's tie key, `city_id` no longer being unique.
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

**The default preset of a pivoted page carries every metric group, and the table scrolls sideways when that does not fit ([#350](https://github.com/jonfroehlich/streetscape-tracker/issues/350)).**
This reverses [#334](https://github.com/jonfroehlich/streetscape-tracker/issues/334), which capped a default at `DEFAULT_PRESET_LEAF_BUDGET = 8` leaves and dropped whole metric groups from the end to keep the table inside the 1500 − 280px measure.
The budget is gone, `fitDefaultPreset` with it; what remains is `withPresetTitle`, which does only the title half (below).

The reason is that the group at the end was always a DATE, and a collection date is not an optional column.
`grid.html` dropped **"Last collected"** from the third provider on, and production has been four deep since Panoramax; `streets.html` dropped **"Walked"** at four and, before [#345](https://github.com/jonfroehlich/streetscape-tracker/issues/345) reordered the preset, **"Median age"** at three.
Street coverage and recency together are what a Project Sidewalk deployment decision reads — coverage with no date beside it is half an answer — and on `streets.html` the loss was total rather than a deferral, since no other streets preset names the age group at all.

**And there was no width to find, which is the measurement that settles it.**
A pivoted leaf is as wide as the PROVIDER NAME in its header, not as the value under it — measured on the live four-provider site: GSV 95px, Mapillary 98px, KartaView 105px, Panoramax 113px — so compacting dates, or numbers, or anything in the cells buys nothing at all.
At four providers the "Walked" group alone costs **375px** against a container of **1100px** that the two remaining groups already fill exactly.
Every lever short of scrolling was measured and none closes a 375px gap: the 12px → 8px cell padding ([#345](https://github.com/jonfroehlich/streetscape-tracker/issues/345)) returns ~80px and is kept anyway, since every px is one the reader does not scroll past.

So the wrap's `overflow-x` becomes the desktop layout rather than the narrow-viewport safety net it was.
ADR 0001 is untouched — this is horizontal scrolling of one element, not pagination or virtualization.
What does NOT change is that **the document itself must never scroll sideways**: the table's width has to stay inside `.streets-table-wrap`, which needs `position: relative` or the header's absolutely-positioned `.visually-hidden` span escapes the scroll container and drags the page with it.
`test_the_page_itself_never_scrolls_sideways` pins that on all three pages, and `driving.html` — one row per PLACE, no provider fan-out, so its width does not grow — keeps the strict fits-its-container assertion in `test_driving_table_still_fits_its_container`.

**The city column is pinned, and that is what makes a scrolling table readable rather than merely wide.**
`position: sticky; left: 0` on the row header and on the header's corner cell, because a scrolled row whose name has gone is unreadable and reading a date AGAINST a named city is the entire point of the columns that made the table wide.
Three things it needs that a bare `sticky` does not give, all of them silent when missed: an **opaque background** (the cell is transparent by default and the scrolled columns slide under it), the row's **hover colour repainted** on it (or the city name is the one cell that does not highlight), and `box-shadow` rather than `border-right` for its edge, since under `border-collapse: collapse` a sticky cell's borders are painted by the table's border grid and do not travel with it.
`test_the_city_column_stays_pinned_while_the_table_scrolls` asserts the behaviour rather than the declaration — `position: sticky` does nothing without a scrolling ancestor, so a `getComputedStyle` check would pass on a page where it never engaged.
It runs at 1440×900 like every other layout test here.
It ran at 1000px until [#354](https://github.com/jonfroehlich/streetscape-tracker/issues/354), because the committed fixture carried three providers where production carried four and three still fit 1440px — so at the wider viewport it would have had nothing to scroll and would have passed having exercised nothing.
The fixture is four deep now, and measured at 1440px the wrap overflows by 174px on `grid.html` and 266px on `streets.html`, so the scroll the test needs is production's own rather than one manufactured by narrowing the window.

**Neither default names a Δ leaf**, filtered on the `isGroupDelta` flag rather than on the "Δ" label — sniffing the glyph would tie a column rule to a character.
That is no longer a width decision: a Δ is one pairwise comparison of two NAMED providers while a metric group is one number for every provider, so a Δ's share of what a row tells you shrinks with each provider added.
`grid.html` needed this stated explicitly, because its `groupKeys` had been including the Δs and only the trim was removing them — retiring the trim would otherwise have silently added three columns the page has not shown since [#334](https://github.com/jonfroehlich/streetscape-tracker/issues/334).
Every explicitly chosen preset keeps its Δ, and so does the column picker.

**A default preset still spells `titleLead` + `titleParts` (group id → clause) instead of a finished `title`, and `withPresetTitle` assembles the sentence.**
A fixed title is an enumeration, and this codebase has watched one go stale the moment a provider count moved ([#295](https://github.com/jonfroehlich/streetscape-tracker/issues/295), [#296](https://github.com/jonfroehlich/streetscape-tracker/pull/296) did it to group titles that named providers): grid's Overview promised "how fresh it is" and, at four providers, showed no age column.
It renders as the preset `<option>`'s hover `title`, so the promise is visible and the missing column is not.
The clause filter survives the trim's removal because it still does real work — a group with no collected providers builds no leaves, and naming it would promise a column that is not there — but the sentence no longer SHRINKS as providers are added, and that shrinking was the symptom of the columns going.
Clauses are Oxford-joined in `titleParts` key order, which on both pages now matches the order the header renders: [#346](https://github.com/jonfroehlich/streetscape-tracker/pull/346) had written `age` ahead of `walked` in the streets preset to steer the trim, which read PRESET order, while the header has always read the column registry's — a mismatch that was invisible while it only moved a trim and is merely misleading now.
A preset carrying a plain `title` (every non-default one) is left strictly alone, identity return included.

**Two lessons from #334 are kept even though its rule is gone**, because both are about tests rather than layout.
The gate that should have caught the original overflow was green against a payload narrower than production's: the e2e fixture carried two providers while production had been three deep since KartaView ([#248](https://github.com/jonfroehlich/streetscape-tracker/issues/248)).
That happened twice — three against production's four when [#350](https://github.com/jonfroehlich/streetscape-tracker/issues/350) landed — so the second lesson is now enforced rather than remembered: `tests/test_e2e_fixture.py` fails, in the **fast** suite, unless the committed fixture carries a grid run and a road walk for every `naming.KNOWN_PROVIDERS` entry on one city, with omissions named and justified in `build_fixture.FIXTURE_OMITTED_PROVIDERS` ([#354](https://github.com/jonfroehlich/streetscape-tracker/issues/354)).
And **"fits" is not the same assertion as "says anything"** — that same gate was green throughout the period a date group was missing, because a trimmed table fits by construction.
`test_streets_default_view_shows_median_age_and_walk_dates` and `test_grid_default_view_shows_when_each_provider_last_collected` are the assertions that encode what the pages must SAY: each date group present, with a populated cell under it.

**The city label renders the state and country as CODES — "Dublin, IN, US", not "Dublin, Indiana, United States" — and that is the other half of the width budget.**
Nothing computes them: `json_summarizer.py` has always published `state.code` (`get_state_abbreviation`) and `country.code` (`get_country_code`, ISO alpha-2), and `cityDisplayLabel` simply prefers them, falling back to the full name wherever a code is absent.
The fallback is what makes the change invisible to the existing suite — every test fixture predates the codes — and it is also what keeps a non-US city honest: `get_state_abbreviation` returns the name unchanged outside the US, so "Paris, Ile-de-France, FR" abbreviates only the half that has an abbreviation.
It lives in `streetscape-utils.js` rather than `table-utils.js` because `index.html` loads only the former; index.js had been carrying a hand-copied duplicate of the same four lines, which is exactly why that page went on spelling names out after the tables stopped.
**`cityFullLabel` is the spelled-out twin, and both are load-bearing**: the abbreviated one is the visible cell, the full one is the `title` tooltip AND a `fullLabel` row field in the search lists, because abbreviating the only searchable string would have quietly stopped "Indiana" matching Dublin.
**(8) A grouped leaf's header button carries `pickerLabel` as its `aria-label`.**
The visible leaf label is a bare provider name repeated under every metric group, so grid's default preset exposes eight sort buttons under **three** distinct accessible names, in one tab order and one rotor list.
Reading the *table* is fine — AT associates the `scope="colgroup"` cell with the body cells during table navigation — but a controls list gets the button's accessible name and nothing else, and the disambiguating text lived only in a hover-only `title`.
The column picker's flat checkbox list hit the identical problem one layer over, and `pickerLabel` is the string it already computes for it.
Emitted only where a descriptor supplies one, so driving.html — whose columns are ungrouped and carry no `pickerLabel` — emits no `aria-label` at all, and its header markup is unchanged.
**(9) A `groupTitle` is also every LEAF's default tooltip, so it must state what the group measures and never enumerate who is in it (#295/#296).**
`providerColumnGroup` hung one shared string on each leaf, so "Including flat/perspective imagery (Mapillary)" was attached verbatim to KartaView's any-imagery column — the provider whose flat imagery is the *larger half* of its data (Yogyakarta: 1,071,155 flat images against 16,913 panos, 16.3% any-imagery coverage against 2.1% 360°) — and the same sentence sat on GSV's, which publishes no flat imagery at all.
The fix is an optional `leafTitle(provider)` hook falling back to `groupTitle`, plus the rule that a per-provider tooltip is **derived from a registry capability, never from a provider name**: `anyImageryLeafTitle` lives in `table-utils.js` because grid.js and streets.js both need the identical `hasFlatImagery` branch and copying it is how the defect reached two pages.
A group title that enumerates does not merely go stale — it misattributes, and it does so on the leaf rather than in the header where the enumeration was written.
Two tooltips carry a fact the numbers beside them do not: the panorama leaf's `(sample)`/`(census)` parenthetical, the one thing telling a reader a census count and a sampled count are not subtractable (which is also why that group has no Δ), and its copyright clause, because `adaptCityRecord` resolves `pano_count` to the official-fleet subset for a `hasCopyrightFilter` provider while the coverage columns in the same row count every PRESENT point regardless of copyright.
The regression guard is a sweep — no per-provider leaf tooltip may name a *different* provider — run over the capability flags as well as the providers, since a sweep reading the registry's own values evaluates one branch per provider and leaves the other free to hardcode the very name it would be describing.

## Histogram-slider filters replaced the distribution strip (issue #250)

*Written after the split.*

**Per-filter histogram-sliders replaced the sorted-column distribution strip.**
The strip visualized the ACTIVE SORT COLUMN over the CURRENTLY FILTERED rows, which made it change its own meaning twice over: re-sorting silently swapped its metric, and clicking a bar filtered the rows the strip was drawn from, so the picture collapsed under the very interaction it invited.
#250 took it off the two pivoted pages and left it on driving.html; driving.html has since moved to the sidebar too, and the strip is now **deleted from the chassis** rather than switched off per page — see the section below.
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
**What protected driving.html through #250 was enforcement, not care**, and the surviving half of that is still load-bearing: `theadHtml` emits exactly the pre-#250 single `<tr>` when no visible column carries a `group`, and a test compares the two strings rather than trusting the eye.
driving.html is still the page that renders through it — its rows are places, not a pivot — so that branch has a real caller and a real browser test.
The other half is gone with the page's old layout: `controlsHtml` carried a second `layout: "inline"` branch, verified byte-identical against `origin/main` over driving.js's real descriptors, with a literal `"\n      "` standing in for the old `${filterControls}` interpolation.
The CSS scoping under `.with-sidebar` / `.streets-main--wide`, which is what kept driving.html's old strip untouched, is likewise gone — every table page got it, so it became the base rule.
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
The mechanism is a descriptor opt-in — `fieldFor`/`labelFor`/`testFor(values)`, resolved by `resolveFilters` into the live view that `applyFilters`, `rowsExceptFilter` and the histograms all read — and **a descriptor declaring none of them passes through by IDENTITY**, which is what keeps driving.html's filters unaware of any of it: a driving row is a place with one GSV number, so there is nothing there to scope.
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
The table pages put search/selects/columns/sliders/checkboxes in a ~280px column beside the table (page measure 1200 → 1500px) that collapses to a "Filters" disclosure at ≤900px; native semantics give keyboard and AT support for nothing.
Above the breakpoint the `<summary>` is `display: none` and the panel is simply a column — which means a panel collapsed on a narrow screen and then widened would be closed with its only toggle gone, stranding filters that are in the URL and cannot be seen or changed.
`wireSidebarDisclosure` re-opens it on widening, one-way (narrowing never closes what the reader opened).
`controlsHtml` orders the sections search → selects → columns → numeric windows → booleans → clear, partitioning by filter TYPE with an "everything else" bucket so a type added later renders in the wrong place rather than not at all
— it took a `layout` option to pick between this order and driving.html's old horizontal one until that page moved here too;
below the breakpoint the same controls become a `repeat(auto-fit, minmax(220px, 1fr))` GRID rather than a wrapping flex row, since a tall wide histogram-slider and a short narrow select interleave into a ragged block under `flex-wrap`.
**`.table-sidebar` itself carries the card chrome and the sticky full-viewport height**, with `.table-controls` transparent inside it
— the obvious alternative, a flex chain stretching the controls to fill, does not survive contact with `<details>`: modern Chromium gives it a `::details-content` box, so `.controls-region` is not a flex item of the disclosure at all and simply does not grow (measured: sidebar 886px, controls 637px).
**And the pages lead with one sentence, not a screen of prose** (`.page-head` / `.page-lead` / a closed `.page-about` disclosure): these are instruments rather than articles, and the preamble was pushing both the table and its filters below the fold.
driving.html held out on a full three-paragraph `.streets-intro` on the grounds that its verdicts do not mean anything until the plan-vs-observed contradiction has been explained; it now leads with one sentence like the others, with that explanation one click away rather than gone.
`.streets-intro` had no other user and went with it.

## All three table pages share one layout, and the alternatives are deleted

*Written after the split.*

**driving.html now renders the same sidebar, page head and histogram filters as grid.html and streets.html, and the two shapes it used to be the only caller of are gone.**
#250 rebuilt the pivoted pages and deliberately left driving.html alone; the result was one page plus two rather than three pages over one chassis, which is the opposite of what "configuration over a shared chassis" is supposed to buy.
What moved: the wide measure and the `.table-layout` markup, the compact `.page-head` with its closed `.page-about` disclosure, and `histogram-range` on all three numeric filters.
Once all three pages carried them, the `.streets-main--wide` and `.with-sidebar` modifiers were folded into their base rules and the sidebar chrome moved into `createTableControls` — a class every caller sets is a base value wearing a class name, and its failure mode was silent (a fourth page forgetting one lands on a strip layout nothing has rendered since, with `wireSidebarDisclosure` returning null and no error anywhere).
What did NOT move is the pivot — a driving row is a **place** (a tracked city, or a plan area covering none), so no column declares a `group`, `theadHtml` still emits its flat single `<tr>`, and there are no Δ cells and no provider scope.
The sidebar and the pivot were separable, and only one of them was ever about providers.

**Two chassis features had no remaining caller afterwards, and were deleted rather than kept warm.**
The sorted-column distribution strip (`renderDistributionStrip`, `formatStripSummary`, `medianOf`, `bucketCountFor`, `showDistributionStrip`, the bar-click handler and the `.strip-*` CSS) went, along with `controlsHtml`'s `layout: "inline"` branch and the `layout` option itself.
An alternative layout nothing renders is one nothing tests either, and the strip in particular carried a live footgun: `histogramBuckets` took a `domain = null` default that scaled the axis to the values, which is exactly right for a strip describing the rows in view and exactly wrong for a slider whose handles must not move under the reader's hand — the two-axes bug above was one call site away the whole time.
`domain` is now required, and there is one axis rule instead of two.

**A third had none either, and the argument for keeping it did not survive review: `type: "range"`.**
The case for keeping the bar-less flavour warm was that the tests making `histogram-range` trustworthy asserted it against a plain `range` TWIN in unset/pass/parse/serialize, so deleting `range` would delete the comparison pinning them as one value shape.
That was wrong in a way worth recording: every one of those parity assertions dispatches through `isRangeType` FIRST, so `f(hist) === f(plain)` compared a branch against itself and could not fail.
The twins are replaced by typed expectations (the `min~max` wire format written out, not compared), the shared test fixture now declares `histogram-range` like the real pages do, and the render branch, `.control-range` CSS and second `isRangeType` arm are deleted.
What survives is the three per-page assertions that each numeric filter IS a `histogram-range` — more load-bearing now than before, since a descriptor left saying `range` no longer renders two number inputs, it falls off the end of `controlsHtml`'s type partition and renders as a **checkbox** for a `{min, max}` value.
`isRangeType` itself stays as a one-arm predicate: it names why nine call sites are grouped (they reason about the value, not the widget).

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
## The basemap needs a CARTO key, and a bad key looks like a styling bug

*Written after the split.*

**The basemap needs a CARTO key, and every way of getting that key wrong is invisible.**
CARTO began requiring an API key on `basemaps.cartocdn.com` on 2026-08-28, and the enforcement is not an error response:
a keyless request returns HTTP 200 and `image/png`, with `API KEY REQUIRED / carto.com/basemaps/apikey` printed diagonally across the tile itself.
Measured the same day, on our own URL form: keyless, a well-formed but wrong key, and the right key under the wrong parameter name (`api_key=` rather than `key=`) all returned the **byte-identical** watermarked tile, and only `?key=<our key>` returned a different one.
So there is no fetch handling, no `errorTileUrl`, and no console warning that could ever have caught it — it reaches a reader as a map that looks oddly branded, and it reached ours for an unknown period before anyone said so.
**If the maps ever look wrong again, check this first**, and check it with bytes rather than with a status code.

`addBasemapLayer(map)` in `streetscape-utils.js` is the only place the tile URL is built; `index.js` and `city.js` call it and `www/js/__tests__/streetscape-utils.test.js` pins that neither of them contains an `L.tileLayer(` of its own.
That pin is the point rather than tidiness: the two call sites were byte-identical duplicates, and a reintroduced duplicate renders perfectly well — watermarked.
The generated boundary-review viewer (`scripts/boundary_review.template.html`) is a third CARTO map that cannot load the module, so `build_boundary_review.py` reads `CARTO_BASEMAP_KEY` out of the JS and substitutes it, raising if the const or the placeholder has gone.
A watermark there is not cosmetic either — that tool exists to judge a city boundary by eye.

**The key is public by construction and bearer-style, which are two different facts.**
Public by construction: the browser sends it to CARTO on every tile request, so it is readable off the deployed page wherever the repo keeps it, and there is no build step to inject it at ([ADR 0001](adr/0001-no-backend.md)).
Bearer-style: CARTO asks for a domain when issuing the key but does **not** enforce it — a tile request with a mismatched `Referer` and one with no `Referer` at all returned byte-identical keyed tiles — so unlike a conventionally domain-locked Mapbox or Google JS key, a copy scraped out of this public repo works anywhere.
That is the part worth acting on: exposure is unavoidable, abuse is not, and the ask is for CARTO to enforce the domain they already collected.
The free ceiling is 5M tile requests per calendar month across the raster **and** vector services, conditioned on crediting CARTO and OpenStreetMap — which is why `addBasemapLayer` sets the attribution, and why exhausting the ceiling degrades to the same silent watermark.
Rotation is a reply to the issuing email plus editing the one const.

**The detection path is `tests/e2e/test_basemap_key.py`**, marked `e2e` so it stays out of the fast no-network suite.
It fetches one tile with the key and the same tile without, and requires the bytes to differ.
Differential rather than a pinned watermark hash, so it survives CARTO restyling the notice, and it goes red for a revoked key, an exhausted quota, and a dropped or misspelled parameter alike.
It also goes red if CARTO ever stops watermarking keyless requests, which is a false alarm worth having: the constraint this mechanism exists for would have changed.
Whether to leave raster for vector is a separate, larger question — vector needs the same key, and Leaflet cannot draw MVT at all — and is worked through in [`experiments/carto-basemap-key.md`](experiments/carto-basemap-key.md).

## The KartaView pano link opens an error page, and the URL is correct (issue #312)

*Written after the split.*

**Clicking a KartaView pano dot sends a reader to "Ups! Sequence cannot be loaded…", and the link is right.**
`PROVIDERS.kartaview.viewerUrl` builds `kartaview.org/details/{sequence_id}/{sequence_index}`, which is the form KartaView's own single-page app writes into the address bar as a viewer session moves between photos (`updatePageUrl` in their `main.*.js`).
The pano behind a failing link is real and fully processed, and `/2.0/photo/?sequenceId=…&sequenceIndex=…` returns it with live CDN URLs.
What fails is the single v1 call that page depends on — `POST api.kartaview.org/details`, which answers `osv: null`, and the console error (`Cannot read properties of null (reading 'photos')`) is that null being dereferenced.
Measured 2026-09-02 over all 38 sequences a Krabi run links to **plus KartaView's own documented example sequence**: 0 of 39 load, our credential changes nothing, and the v2 endpoints on the same host are healthy ([`experiments/kartaview-viewer-deeplink.md`](experiments/kartaview-viewer-deeplink.md)).

**Their own example failing is why this is written down here rather than fixed in the registry.**
A broken third-party page and a malformed URL present identically, so the natural next move — rewriting `viewerUrl` — edits code that is already correct, and no test can catch that because the "fix" would be just as unverifiable against a backend that refuses everything.
Before touching a deep-link builder for any provider, run **their** canonical example through the identical call.

**The popup therefore carries two links, and the order is the fix.**
`viewerLinksHtml(provider, panoId, row)` in `streetscape-utils.js` renders `fallbackViewerUrl` first and `viewerUrl` second, dropping either when its builder returns null; both popup builders in `city.js` call it and build no link of their own (pinned by a source check in `www/js/__tests__/streetscape-utils.test.js`, the same shape as the `addBasemapLayer` pin above).
It lives in the registry module rather than in `city.js` because which viewer can be trusted is a property of the registry — and because `city.js` builds a Leaflet map at load, so nothing in it can be unit-tested.
For KartaView the fallback is `kartaview.org/map/@{pano_lat},{pano_lon},19z`: their map view is served by the v2 stack that does answer, and their coverage tiles were measured serving content to z20, so z19 frames the pano's own track rather than a neighbourhood.
GSV and Mapillary declare `fallbackViewerUrl: null` and render one link exactly as before.

**The fallback is keyed on geometry, which makes it cover strictly more rows than the link it backs up.**
Every linkable row carries `pano_lat`/`pano_lon` — `OK`, `NO_DATE` and `FLAT_ONLY` all populate them, and only `ZERO_RESULTS` is blank, which never gets a link — so a row with a null `sequence_id`, which could never build a photo link at all, now gets one link instead of none.
That is why `buildFlatOnlyPopupHtml` asks for its links **before** its missing-image-id early return rather than after: an id-less flat row still has a position, and gating the call on the id is the one row shape where the invariant would silently stop holding (pinned by a source-order check).
`vis.PROVIDER_DISPLAY` carries the same two links in the same order for the folium run map; the two registries are hand-maintained copies, and only the JS one is what a visitor clicks.
**Every `PROVIDER_DISPLAY` entry spells its own `viewer_label`, with no `View in {label}` default to inherit** — #312 earned that rule with a link that opens an error page, and a default would have described it as "View in KartaView".
If KartaView repairs `/details`, the change is re-ordering two entries and dropping the caveat from `viewerLabel` — re-run `scripts/kartaview_details_probe.py` first, and confirm in a browser, since the probe measures the call and not the page.

**Panoramax links the federation's own viewer, `api.panoramax.xyz/?focus=pic&pic={pano_id}`, and has no fallback — the opposite outcome from KartaView's, reached by the same method.**
The rationale this replaced was reasoned rather than measured, and wrong: it held that the meta-catalog "hosts no viewer, its root is a marketing page" and that a picture could not be opened without first naming which of the 23 federated instances owns it, so `vis.PROVIDER_DISPLAY` linked the raw JPEG at `/api/pictures/{id}/sd.jpg`.
Probed 2026-09-10 ([#334](https://github.com/jonfroehlich/streetscape-tracker/issues/334)): `https://api.panoramax.xyz/` answers 307 to `/en/index`, whose body embeds `<pnx-viewer endpoint="/api" metacatalog="false">` — the official `@panoramax/web-viewer` bound to the meta-catalog; its permalink parameters are query-string rather than hash (`pic=`, `focus=`, `map=`, per `docs.panoramax.fr/web-viewer/03_URL_settings/`) and survive the 307; and `GET /api/pictures/<uuid>` answers 200 for a picture from our own run, so the meta-catalog resolves a picture without being told its instance.
Browser-verified on a real picture before shipping, which is #312's lesson applied **before** rather than after — and is why there is no fallback: a fallback is for a provider whose own viewer cannot be trusted, and this one can.
The `withFallback === ["kartaview"]` pin in the node suite is what keeps a later entry from copying the fallback rather than earning one.

## A filename resolves to a provider or to nothing — never to a default (issue #338)

**`getProviderFromFilename` returns `null` for a provider token this build does not know, and `isValidRunFilename` is DEFINED as that lookup succeeding.**
Until [#338](https://github.com/jonfroehlich/streetscape-tracker/issues/338) it returned `"gsv"` for an unrecognised token, which is what turned [#334](https://github.com/jonfroehlich/streetscape-tracker/issues/334) from a failure into a misrender: `city.js` drew 135,389 Panoramax pictures under Google's attribution, the GSV colour ramp and GSV's 2007 capture-date floor, offered a "Google only" radiogroup whose default mode hid **every** marker because no Panoramax row is `© Google`, and built `google.com/maps/...pano=<uuid>` links to nothing.
Nothing on that page looked broken, which is the cost of the default: a hard failure would have been caught in minutes.

**The fix separates two cases the old regex could not tell apart.**
A name with **no token at all** still resolves to `gsv` — that is the naming contract (`docs/architecture.md`) and it must not change, because pre-2026 undated files carry no token and their published URLs have to keep working.
A name with a token **that is not in `PROVIDERS`** resolves to `null`, matching what `naming.parse_filename` has always done on the Python side: it raises `ValueError` on an unknown token rather than substituting `DEFAULT_PROVIDER`.
A name that is not a run filename at all — a diff, `cities.json.gz`, a traversal attempt — also resolves to `null`, where it used to answer `"gsv"` as confidently as a real Street View run.

**`RUN_FILENAME_RE` is a deliberately STRICTER subset of `naming.FILENAME_RE`, not a mirror of it**, and the JSDoc says so, because a reviewer reading "mirror" will eventually find the differences and file them as bugs.
The two answer different questions: Python parses any name the project has ever written, from any path and any extension, while this validates a URL a stranger just handed the browser.
Four narrowings, all intended — `.csv.gz` only (Python also strips `.json.gz`, `.csv`, `.json`, `.html` or nothing at all), no path component (Python takes the basename; here a separator IS the traversal attempt), no `#`, and integer `_width_`/`_height_` where Python accepts `_width_5000.0_`.
A fifth exclusion, `\n` in the slug, is **alignment rather than narrowing**: a negated character class matches a newline and Python's `.` does not, so an embedded newline resolved a provider in JS and raised `ValueError` in `parse_filename` — measured with real `node` after the fact, and fixed in the JS class because Python is the stricter of the two and nothing legitimate carries a newline.
The float case was checked rather than assumed: **0 of 2,390 names on disk and 0 of 1,161 in the published aggregate** carry float dimensions, and the pre-#338 validator did not accept them either, so nothing changed.
One divergence survives, and the first telling of it here was backwards: an impossible date like `_2026-13-45` is shape-valid in JS and raises in Python, which builds a real `date`.
That is a **direct counterexample** to the invariant below — JS resolves `gsv`, Python refuses — not a case running the other way.
It reaches nothing, because such a name resolves to a file that cannot exist, and the cross-language test now pins it explicitly so the exception stays visible instead of being excluded from the corpus.

**What must hold is the overlap, and it is pinned across the language boundary**: `test_the_js_run_filename_regex_agrees_with_python` (`tests/test_naming.py`) reads `RUN_FILENAME_RE` out of the JS source, runs it as a Python pattern, and asserts that any name it resolves to a provider, `parse_filename` resolves to the SAME provider.
It sweeps `KNOWN_PROVIDERS` through `generate_run_filename` so a new provider is covered the day it is added, and it counts how many names actually resolved — a regex that matched nothing would otherwise make it vacuously green.
Verified to fail on three real drift modes: dropping the capture group (the #338 shape, where a tokened name reads as gsv), renaming the const out from under the anchor, and re-admitting `\n` to the slug class.
Be exact about its reach, since this PR's own thesis is that a contract in two languages drifts silently: it runs the JS pattern **text** through Python's engine, so it can never see a construct the two engines read differently.
One such construct is live — JS's `$` without `m` is end-of-input while Python's also matches before a trailing newline — and the test translates it to `\Z` rather than leaving the port more permissive than the browser.
The direction is deliberately one-way; demanding equality would fail on exactly the hostile inputs the narrowings exist to reject.

**One regex, `RUN_FILENAME_RE`, now backs both functions.**
`isValidRunFilename` had its own copy of the contract, and the two copies disagreed in exactly the way that mattered: the validator accepted `_notaprovider_` and the lookup then renamed it gsv.
Defining the validator as `getProviderFromFilename(name) !== null` makes them one decision, so a later widening of either cannot reopen the hole; `test_isValidRunFilename: is exactly 'getProviderFromFilename resolved'` pins the equivalence over a list of names rather than trusting the implementation to stay that way.

**Both call sites refuse rather than degrade, because there is no degraded render available.**
`city.js` reads the registry entry for attribution, colour ramp, capture-date floor, viewer link and the copyright toggle, so substituting gsv does not produce a partial page — it produces a complete and plausible page about the wrong provider.
A rejected `?file=` therefore gets its own message naming the file, instead of falling through to the generic "No city specified", which would send the reader looking for a missing parameter.

**A `?file=` that was present and rejected is refused even when `?city=` would resolve something** — the hole a review found in the first version of this fix.
Refusing only when `?city=` was absent left `?file=<a panoramax run>&city=Ames` rendering Ames's GSV series instead: a complete, error-free page under Google's attribution, in answer to a URL that named a non-Google run, with nothing but a `console.warn` to say so.
That is the #338 substitution reached through a URL shape the fix did not cover, so the fallback is gone.
It costs nothing, because **no link builder in `www/` emits both parameters** — `index.js`, `grid.js`, `streets.js`, `driving.js` and `city.js`'s own snapshot selector all emit `?file=` alone — so only a hand-edited or stale URL reaches it, and there naming the fault beats quietly rendering something else.

**The aggregate refusal is defence in depth against a malformed payload, NOT a route a healthy site takes** — the docs said "mid-deploy" first, and that was wrong in an instructive way.
`data_file.filename` is read out of `providers[<key>].latest`, where the key comes from `Object.keys(PROVIDERS)`, so a frontend that lacks a provider never asks for its view and therefore never sees its `data_file`; a stale cached bundle cannot reach this branch, it is the very thing that hides it.
What CAN reach it is a published record whose filename disagrees with the provider block holding it.
The branch stays, because a payload bug is exactly when a plausible wrong page is most expensive — but the aggregate path and the `?file=` path now word their messages differently, and a `targetFile` that is missing entirely says so rather than blaming a provider this page does not know.

**The echoed value is quoted and capped** (`quoteForMessage`, 96 code points).
`?file=` is attacker-chosen, and naming it in the refusal is what makes the message useful — but an unbounded echo lets a crafted link render hundreds of KB of arbitrary prose on `makeabilitylab.cs.washington.edu`, where "SECURITY ALERT: call +1-555-0100" reads very differently than in a URL bar.
This is **not** an escaping function and does not stand in for one: markup is harmless here because `showLoadError` writes `textContent`, which is the only writer of `#progress-text` besides the progress messages.
It is sliced by code point, so a cap landing inside a surrogate pair cannot emit a lone surrogate — a replacement character in an error message about something being malformed reads as our bug.

The rest of the frontend needs no change and gets none: every fan-out over the registry already gates on presence in the payload (`grid.js`, `streets.js`, `index.js`), so an unregistered provider's rows are skipped rather than mislabelled.
That is the inverse rule — a registered provider is not a collected one — and #334's registry pin (`test_the_js_registry_covers_every_known_provider_too`) is what keeps the collected-but-unregistered state from recurring in the first place.
This issue is about what happens if it does anyway.

`config.PROVIDER_RUN_DTYPES` has the same `.get(provider, DEFAULT)` shape and was checked while here: it is unreachable with an unknown provider.
`fileutils.dtypes_for_run_path` reaches the lookup only through `naming.parse_filename`, which raises on an unknown token, or `naming.parse_streetwalk_filename`, which is safe for a sharper reason — `_STREETWALK_FILENAME_RE` embeds a provider alternation built from `KNOWN_PROVIDERS`, so an unknown token fails the regex rather than reaching `match.group("provider")`.
Left alone deliberately: its fallback catches names the naming contract does not parse at all (fixtures, ad-hoc exports), which is a different question.
The tests around it are `test_dtypes_for_run_path_picks_the_schema_from_the_provider_token` (which is the one that actually calls the function, and has no unknown-token case) plus the two subset pins, `test_a_run_schema_is_reachable_from_a_filename` and `test_every_known_provider_has_a_run_schema`.
