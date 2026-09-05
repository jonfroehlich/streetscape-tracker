# Street-level imagery providers: the candidate inventory

This is the standing list of every street-level imagery provider that could, in principle, become a Streetscape Tracker collection channel — the three we collect, the ones we have ruled out, and the ones still open.

It exists because "what else could we track?" gets asked every few months and was, until now, re-answered from memory each time.
Sibling to [`related-work.md`](related-work.md), which inventories *tools*; this one inventories *sources*.

**Scanned 2026-09-04.** Method and limitations are stated below so this can be re-run rather than trusted.

## What "possible" means here

A provider is a *candidate* only if all four hold.
Failing any one of them is what most of the list below fails on, and the entries say which.

1. **A queryable metadata API.** We need position and capture date per image, over a bounding box or tile, without downloading imagery.
   A provider that only renders panoramas in its own viewer gives us nothing to catalog.
2. **360° imagery, or a per-image field that separates 360 from flat.**
   Not every provider is spherical, and a mixed corpus is fine *if* the mix is machine-readable — that is the [#116](https://github.com/jonfroehlich/streetscape-tracker/issues/116) stratification we already run, which yields two coverage numbers (360° and any-imagery) that are never conflated.
   A mixed corpus with no such field is not usable, because a coverage rate over it means nothing.
3. **Coverage in cities we track.** Our catalog is 1,216 enabled cities, heavily US.
   A provider with 50 M pictures all in one country adds one country, and that has to be worth a channel's operational cost.
4. **An access story we can pace safely.** A documented rate limit, or a community where the undocumented one is discussed.
   This is the [`provider-access.md`](provider-access.md) rule, and it is the criterion that most often decides the answer.

## Method

Five sources: each provider's own developer documentation; the [OpenStreetMap wiki street-level imagery pages](https://wiki.openstreetmap.org/wiki/Street-level_imagery_services); the [`streetlevel`](https://github.com/sk-zk/streetlevel) library's supported-service list, which is the best available census of what has been successfully reverse-engineered; general web search; and, for Panoramax and Apple, direct read-only probes of the live APIs recorded in the entries below.

**Limitations.** English-language and public sources only, so national providers outside the anglophone and francophone web are under-represented, and in-house municipal archives almost entirely absent.
Picture counts and coverage claims are the vendor's own unless an entry says otherwise; nothing here is an independent measurement except where explicitly marked **measured**.
Rate-limit rows saying "none documented" mean none was *found*, which per this repo's standing rule is read as unknown rather than unlimited.

## The list

Kept **alphabetical**, so two branches adding a provider usually insert at different offsets and merge rather than collide.

| Provider | 360°? | Metadata API | Auth | Scope | Status |
|---|---|---|---|---|---|
| Apple Look Around | Yes | Unofficial (`streetlevel`) | None for coverage | ~Global | Open — bounded study only |
| Baidu Panorama | Yes | Unofficial (`streetlevel`) | None | China | Ruled out — scope |
| Bing Streetside | Yes | Retiring | Enterprise key | ~Global, frozen | Ruled out — retired |
| Cyclomedia | Yes | Commercial | Contract | NL, DE, US metros | Ruled out — cost |
| Google Street View | Yes | Official | API key | Global | **Integrated** |
| Hivemapper / Bee Maps | No | Commercial | API key | ~33% of roads | Ruled out — not 360 |
| Já 360 | Yes | Unofficial (`streetlevel`) | None | Iceland | Ruled out — scope |
| Kakao Road View | Yes | Unofficial (`streetlevel`) | None | South Korea | Ruled out — scope |
| KartaView | Mixed | Official-ish | Token | Global, uneven | **Integrated** |
| Mapillary | Mixed | Official | Token | Global | **Integrated**, 360-only |
| Mappls RealView | Yes | **Viewer only** | OAuth2, paid | India | Ruled out — no metadata API |
| Mapy.cz Panorama | Yes | Unofficial (`streetlevel`) | None | Czechia, Slovakia | Ruled out — scope |
| Naver Street View | Yes | Unofficial (`streetlevel`) | None | South Korea | Ruled out — scope |
| Panoramax | Mixed | Official (STAC) | **None** | FR-heavy, growing | **Open — [#316](https://github.com/jonfroehlich/streetscape-tracker/issues/316)** |
| Tencent Street View | Yes | Official (CN) | Key, CN entity | China | Ruled out — scope |
| Yandex Panorama | Yes | Unofficial (`streetlevel`) | None | RU, TR, CIS | Ruled out — scope |

### Apple Look Around

**Status: open, but only as a bounded study — not a nightly channel.**

No official API exists and Apple publishes no coverage information at all, which is exactly what makes a dated series valuable: it would be the only public record of Look Around's extent over time.

[`streetlevel`](https://streetlevel.readthedocs.io/) reaches it through Apple Maps' internal endpoints.
`get_coverage_tile(x, y)` and `get_coverage_tile_by_latlon(lat, lon)` return a list of panoramas per **zoom-17 XYZ tile**, each carrying `id`, `build_id` (the imagery-set revision), `lat`, `lon`, `date` as a UTC capture timestamp, and a **CAR vs BACKPACK** coverage type.
Async variants exist.
Critically, the `Authenticator` is required only to download panorama *faces*; the coverage endpoint we would actually use needs no credential.

`build_id` and the capture type are richer than anything our three current providers expose, and would make change detection unusually precise.

**The blocker is cost geometry.** Zoom 17 is three levels below the z14 tiles our Mapillary census walks, so tile counts are ~56–61× ours for the same bbox (**measured** — computed from the frozen grid bboxes of three catalog cities):

| City | z14 tiles (Mapillary) | z17 tiles (Look Around) | ratio |
|---|---|---|---|
| Seattle | 198 | 11,152 | 56× |
| Oklahoma City | 441 | 26,082 | 59× |
| New York | 702 | 42,640 | 61× |

For scale, the whole Mapillary channel across 29 cities on 2026-09-04 spent 1,723 requests.
One Oklahoma City sweep is ~26,000.

**And the access profile is the worst on this list.**
It is a reverse-engineered internal endpoint that `streetlevel`'s own README says "may break unexpectedly"; there is no documented rate limit; and there is no forum, tracker or mailing list where a block would have been described before we hit one — strictly less early warning than KartaView, which at least has a published figure to pace against.
A break is not an outage here, it is a permanent hole in the series.

If it is pursued, the shape is a one-off study over 5–10 cities at a deliberately conservative pace and **not from the production host**, written up under [`experiments/`](experiments/README.md) — never a scheduled channel over the catalog.

### Baidu Panorama

Reachable via `streetlevel.baidu`, no key.
Coverage is China, where we track no cities; the entry exists so the next scan does not have to rediscover it.

### Bing Streetside

**Status: ruled out — the product is retiring.**

Consumer Streetside was retired in October 2025.
The developer path — Bing Maps `Get Imagery Metadata`, which is how [`streetlevel.streetside`](https://streetlevel.readthedocs.io/) and everyone else reached the bubble metadata — is deprecated: already gone for free accounts, and enterprise accounts lose it on **2028-06-30**, with migration pointed at Azure Maps, which has no street-level equivalent.

Building a temporal series on a provider with a published end-of-life date is the one case where the "a missed month is a permanent hole" rule argues *against* collecting: the whole series would terminate on a known date.

### Cyclomedia

Commercial survey-grade panoramas with contract-only access, priced per seat.
Netherlands, Germany and selected US metros.
Ruled out on cost, not capability.

### Google Street View

Integrated as the default channel.
See [`provider-access.md`](provider-access.md) for pacing and [`architecture.md`](architecture.md) for why the GSV series is a *sample* (nearest pano per grid point) rather than a census.

### Hivemapper / Bee Maps

A token-incentivized dashcam network claiming ~33% of the global road network and millions of km refreshed weekly, with commercial Map Image and Map Features APIs.

**Ruled out because the imagery is not 360.** Capture is front- and side-facing dashcam; 360 rigs are described as a future direction, not a current one.
Worth re-checking on a later scan precisely because that could change, and the refresh cadence would otherwise be the best on this list.

### Já 360

`streetlevel.ja`, no key, Iceland only.
We track no Icelandic cities.

### Kakao Road View

`streetlevel.kakao`, no key, South Korea only.

### KartaView

Integrated as an **opt-in** channel on both grid and street walks.
Mixed 360 and flat; `census_is_pano` is the seam that separates them.
See [`census.md`](census.md) for the four rules that must survive without a read, and [`experiments/kartaview-sweep-cost.md`](experiments/kartaview-sweep-cost.md) for what a sweep costs.

### Mapillary

Integrated, filtered to **360° panoramas only**.
See [`provider-access.md`](provider-access.md) — this is the provider whose undocumented per-IP throttle is the reason this repo has a provider-access doctrine at all.

### Mappls RealView

**Status: ruled out on criterion 1 — there is no metadata API, only a viewer.**

MapmyIndia's (CE Info Systems) indigenous pan-India 360° street imagery.
It is genuinely 360, genuinely national, and backed by a real company with a developer console — which is why it looks like the strongest candidate for India until you read the actual surface area.

**Every public RealView surface renders panoramas; none of them answers "what imagery exists here, captured when?"**
There are exactly two, and they were both checked on 2026-09-04:

- The Web Maps JS SDK exposes a single display toggle, `mapObj.realview(true)`, which turns a layer on and off. There is no query, no response, no metadata.
- The embeddable widget is an `iframe` against `realview.mappls.com/realview_widget/…`, taking a Mappls Pin or a `lat,lng` plus `minDistance`/`maxDistance` in metres, and rendering a panorama for a human to look at.

The public REST API catalog ([`mappls-api/mappls-rest-apis`](https://github.com/mappls-api/mappls-rest-apis)) carries 27 endpoint families — geocoding, routing, snap-to-road, still map images, elevation — and **no** panorama, RealView or street-imagery endpoint among them.

The widget's `minDistance`/`maxDistance` parameters imply a nearest-panorama lookup underneath, which is structurally what our GSV sample does per grid point, so a suitable JSON endpoint very likely exists behind the iframe.
It is undocumented, which puts reaching it in the same category as Apple Look Around rather than in the "official API" column this entry originally claimed.

**It is also explicitly commercial.** The widget documentation states plainly that it "is access controlled and a paid service"; access is OAuth2 with 24-hour tokens, enabled per-account in the Mappls console.
So the cost question and the capability question both land on the same answer, and the capability one is the binding constraint: a paid viewer is still a viewer.

**The one path that would reopen this is a direct ask.** Mappls support (`apisupport@mappls.com`) is the documented channel, and there is no forum — the community surface is the [`mappls-api` Stack Overflow tag](https://stackoverflow.com/questions/tagged/mappls-api).
A research inquiry asking whether a coverage or metadata endpoint exists under an academic or enterprise arrangement is a reasonable thing to send, and is the only step worth taking before this entry changes.
Nothing should be reverse-engineered here in the meantime.

### Why India is the sharpest case for a fourth provider

This sits under Mappls because Mappls is the provider that would have solved it.

We track three Indian cities, and our existing providers are thin to absent there (**measured**, from the catalog on 2026-09-04):

| City | GSV coverage | Mapillary coverage | Panoramax pictures | Panoramax 360° |
|---|---|---|---|---|
| Chandigarh (untracked) | not collected | not collected | 17 | 0 |
| Gurgaon, Haryana | 31.7% | not collected | — | — |
| Luckeesarai, Bihar | 14.2% | not collected | 0 | — |
| Mumbai | 22.2% | 0.11% | 172 | 0 |
| New Delhi | 49.9% | 0.38% | 472 | 0 |

Mapillary is effectively empty in India — two-tenths of a percent is not a coverage rate, it is a rounding artifact.
Panoramax is worse in absolute terms and, decisively, carries **zero** 360° pictures across both metros: everything returned was flat — including the rows with no `field_of_view`, which the phase-1 study showed the tiles type `flat` without exception — contributed almost entirely by the MapComplete and OSM-FR instances.

Chandigarh is included because it hosts a Project Sidewalk deployment and is not yet in the catalog; its 17 Panoramax pictures all came from one instance in one year, none of them spherical.
So for India, GSV is not merely the best provider, it is close to the only one, and its own coverage runs 14–50%.
That is the largest provider gap anywhere in the catalog, and it has no open solution on this list today.

### Mapy.cz Panorama

`streetlevel.mapy`, no key, Czechia and Slovakia.

### Naver Street View

`streetlevel.naver`, no key, South Korea only.

### Panoramax

**Status: phase 1 measured — tracked in [#316](https://github.com/jonfroehlich/streetscape-tracker/issues/316), writeup in [`experiments/panoramax-feasibility.md`](experiments/panoramax-feasibility.md).**
The median tracked city holds nothing (730 of 1,144 screen to a conclusive zero), but ~20 cities hold 16k–1.1M pictures and Des Moines has more 360° pictures on Panoramax than in Mapillary's census of the same bbox — so the recommendation is an **opt-in** channel on the KartaView pattern, never a default-membership one.

A federated open imagery commons founded by IGN and OpenStreetMap France, licensed per picture, with **25 registered instances** and ~100 M pictures.
Two instances hold ~99% of them — IGN at 58.0 M and OSM-FR at 54.0 M — with OSM-HR (4.2 M), Taiwan (1.2 M) and Belgium (0.65 M) next and a long tail of hobbyist servers down to a few thousand pictures.

**The federation is not our problem.** [`api.panoramax.xyz/api`](https://api.panoramax.xyz/api) is a meta-catalog that harvests metadata from every registered instance, so we query **one host** no matter how many instances join.
That means one entry in `host_lock.py`, not 25.

**It is a STAC API**, unauthenticated for read.
`/api/search?bbox=…&limit=…` works with no credential, and a vector-tile endpoint `/api/map/{z}/{x}/{y}.mvt` exists — structurally the same shape as Mapillary's tile census, so `census.py`, `checkpointing.py` and the [#290](https://github.com/jonfroehlich/streetscape-tracker/issues/290) cache would parameterize onto it rather than needing a new pipeline.

Per-picture metadata is **richer than either census provider we have**: `datetime` (capture), `created`/`updated` (ingest — so KartaView's `shot_date >= date_added` guard comes from real fields rather than inference), `license`, `geovisio:producer`, `quality:horizontal_accuracy`, `pers:interior_orientation.field_of_view`, and a `via` link naming the source instance.

**It is not all 360, and the mix varies enormously by city** (**measured** 2026-09-04, first 2,000 features per bbox from the federated search — indicative, not a random sample):

| City | n | 360° | flat | field absent |
|---|---|---|---|---|
| Seattle | 2,000 | 89.5% | 0.1% | 10.5% |
| Paris | 2,000 | 75.3% | 15.7% | 5.4% |
| Taipei | 2,000 | 65.9% | 0% | 34.1% |
| Brussels | 2,000 | 56.8% | 6.0% | 3.1% |
| Zagreb | 2,000 | 0.0% | 99.7% | 0.3% |

Zagreb is the instructive one: the OSM-HR instance's 4.2 M pictures appear to be entirely flat dashcam capture, so a 360-only filter would discard the whole instance.
Each city is also dominated by one or two contributors — Seattle is 91% a single GoPro Max user — so the mix is a property of who happened to map there, not of the platform.
The consequence for a collector is that Panoramax must report **both** coverage numbers under #116.
"Field absent" is **not** a third state: the phase-1 study looked 2,136 EXIF-less search pictures up in the tile `pictures` layer, whose `type` has no absent state, and every one is `flat` — the absence is the search endpoint's EXIF passthrough, and a tile-based collector never sees it.

**Rate limits are not documented** anywhere found, including the OpenAPI spec, and a single read-only probe returned no rate-limit headers.
Unlike KartaView, though, **a staffed community exists** — [forum.geocommuns.fr](https://forum.geocommuns.fr) and the [OSM community forum](https://community.openstreetmap.org/), with core developers answering within days — so the standing rule's "read the forum first" has an object here, and the pacing question can simply be *asked* before any collector is written.

**One open risk.** Coverage against the catalog is now measured (above).
But there is active work on migrating sequences between instances, so picture identity across instance moves needs an answer before we build diffs — if an image can change instance and identity, "removed" in a run-to-run diff could mean "migrated", which would corrupt the one statistic this project exists to produce.

### Tencent Street View

Official Chinese API requiring a mainland business entity.
Scope is China.

### Yandex Panorama

`streetlevel.yandex`, no key.
Russia, Turkey and CIS states, where we track no cities.

## Evaluating a new provider

The order matters, and it is the one [`provider-access.md`](provider-access.md) argues for: the cheap disqualifying questions come first, and nothing is collected until the access question is answered.

1. **Read the provider's own docs and its community forum.** Before writing code, not after a failure.
   Record which of the two the rate limit came from; a documented number that no forum corroborates is a starting point, not a budget.
2. **Establish the 360 story.** Is there a per-image field? What are its states, including absent?
3. **Price one sweep from a real frozen grid bbox**, not a hypothetical city — tile counts scale with the square of zoom depth, and that is where a provider becomes unaffordable.
4. **Measure coverage on a stratified sample of the catalog** before writing a collector, and write it up under [`experiments/`](experiments/README.md) even if the answer is no.
   A negative result here saves a channel's worth of operational cost.
5. **Only then** decide between a default-membership channel and an opt-in one, and give the answer a `CHANNEL_DEFAULT_MEMBERSHIP` entry — a missing entry must stay a `KeyError`, never a permissive default.
