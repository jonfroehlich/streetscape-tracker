# Related work

This is the citable record of what else exists in this space and how Streetscape Tracker differs.

This scan was run **2026-09-01**; the method below is stated so it can be re-run rather than trusted.

## Method

Five source families were searched: the GitHub repository search API (≈15 query shapes over names, descriptions and READMEs, sorted by stars); general web search; the OpenStreetMap wiki [Category:Street-level imagery](https://wiki.openstreetmap.org/wiki/Category:Street-level_imagery) and [Street-level imagery services](https://wiki.openstreetmap.org/wiki/Street-level_imagery_services) pages; arXiv; and Crossref, which supplied every bibliographic record in [References](#references).

**Limitations.** The scan was English-language and public only, so closed commercial products, municipal in-house tooling and non-English projects are systematically under-represented.
GitHub search ranks by stars, which hides new work — [tracelines](#multi-provider-coverage-extraction) surfaced at zero stars and would have been missed by a threshold.
Claims below are therefore of the form "no tool was found that…", never "no tool exists".

## Positioning

**sv-map tracks Google coverage over time but not its freshness; ZenSVI analyzes freshness but not over time; no tool was found that does both, across providers, per city.**

The enabling difference is not the collection code, which is comparable to several projects below, but the **frozen grid**: geometry is computed once per city and never re-derived, so two runs sample identical points and a difference between them is a change in the imagery rather than an artifact of re-geocoding.
Every property in the table follows from that decision.

| | Providers | Repeated collection | Capture dates | Per-city coverage rate |
|---|---|---|---|---|
| **Streetscape Tracker** | GSV, Mapillary, KartaView | Dated snapshots on a frozen grid, ~quarterly per city | Published as first-class statistics | Cross-provider comparable on one sample frame |
| sv-map | GSV | Daily raster archive since 2022-01-31 | No | No |
| tracelines | GSV, Mapillary, KartaView | No | Median year per density probe | No — output is polylines |
| ZenSVI | Mapillary, KartaView, Amsterdam | No | Per image, street or grid cell | No |
| global-streetscapes | Mapillary, KartaView | Static dataset release | Per image | No |
| streetlevel, streetview-dl, robolyst/streetview | GSV (streetlevel adds Look Around, Streetside, Yandex) | No | Per panorama | No |
| Google Street View Insights | GSV | Vendor-internal | Imagery back to 2019 | Not exposed |

## Tools

### Longitudinal coverage archives

[**sv-map**](https://sv-map.netlify.app/) ([dataset](https://sv-map.netlify.app/dataset)) is the only project found that is genuinely longitudinal.
It archives Google's blue coverage-line tile layer daily since 2022-01-31 and publishes each day as a PMTiles archive of blue-pixel PNGs, so two dates can be differenced to find newly covered roads.
It carries no coordinates, capture dates or per-city statistics, and covers no provider but Google.

It is complementary rather than competing, in two ways worth acting on: it is an **independent check** on our GSV coverage deltas, and its archive **predates our series** by a span we cannot backfill.

[**Virtual Streets**](https://virtualstreets.org/) reports new Street View coverage editorially, as blog posts rather than structured data.

### Multi-provider coverage extraction

[**tracelines**](https://github.com/Prekzursil/tracelines) is the closest in shape.
It extracts continuous official-vehicle coverage from all three of our providers as GeoJSON polylines, deliberately excluding user photospheres — the same official-versus-contributed distinction Streetscape Tracker enforces through an exact `© Google` copyright match.
It is a one-shot extractor: no dated series, no diffs, no scheduling.

### Street view imagery analysis toolkits

[**ZenSVI**](https://github.com/koito19960406/ZenSVI) (Ito et al., 2025) is the most mature software in the field: an end-to-end pipeline spanning download, metadata analysis, computer vision, reprojection and visualization, with metadata computed at image, street or **grid** unit.
Two boundaries matter here.
It does not support Google Street View at all, and its metadata analysis describes a single collection — there is no dated snapshot or run-to-run diff.

[**global-streetscapes**](https://github.com/ualsg/global-streetscapes) (Hou et al., 2024) is a static release of 10 million Mapillary and KartaView images across 688 cities with 346 attributes.
It is a dataset, not a re-runnable series.

Both come from the same lab, which is the group most likely to converge on this territory.

### Acquisition primitives

These sit **below** Streetscape Tracker rather than beside it, and one is already a dependency of tracelines:

- [robolyst/streetview](https://github.com/robolyst/streetview) — current and historical GSV panoramas.
- [sk-zk/streetlevel](https://github.com/sk-zk/streetlevel) — panoramas and metadata from GSV, Look Around, Streetside and Yandex.
- [stiles/streetview-dl](https://github.com/stiles/streetview-dl) — grid-based area sampling with a metadata-only mode.
- [sutd-visual-computing-group/google-streetview-gis-stack](https://github.com/sutd-visual-computing-group/google-streetview-gis-stack) — OSM road sampling into GSV metadata to build coverage maps; unmaintained since 2021, and effectively a prototype of the GSV half of this project.

### Vendor analytics

Google now offers [**Street View Insights**](https://developers.google.com/maps/documentation/street-view-insights/coverage) on Maps Platform, with imagery back to 2019.
It is sales-gated, single-provider, and limited to six countries (Canada, Ireland, Japan, Romania, the United Kingdom, the United States), so it does not currently overlap — but it is the vendor entering the measurement business and is worth re-checking on each refresh of this scan.

## Literature

**Capture dates.** Curtis et al. (2013) is the direct ancestor of our capture-date work: it showed that GSV imagery dates are spatio-temporally unstable, so imagery adjacent in space may be years apart in time, and that a study treating a city's imagery as one epoch is mismeasuring it.
That paper covered a single city by hand; the same question at corpus scale is what `analysis.dated_unique_panos` exists to answer (see [`capture-dates.md`](capture-dates.md)).
Wang et al. (2024) is the nearest longitudinal use of GSV, applying historical imagery to perceptual quality over time rather than to coverage.

**Coverage and bias.** Fan et al. (2025) measures how completely SVI observes building facades in London using isovist analysis, and explicitly does not analyze recency or release a tool.
Alpherts et al. (2025) shows that city layout itself biases what street view captures across 28 cities, which is an argument for reporting coverage per city rather than pooled — the shape our per-city rates already take.

**Providers.** Mahabir et al. (2020) is the precedent for comparing crowdsourced providers, contrasting Mapillary and OpenStreetCam (now KartaView) on spatial coverage and contribution patterns.
Biljecki and Ito (2021) remains the standard review of the field.

## References

Alpherts, T., Ghebreab, S., van Noord, N. (2025). Artifacts of Idiosyncracy in Global Street View Data. arXiv:[2505.11046](https://arxiv.org/abs/2505.11046).

Biljecki, F., Ito, K. (2021). Street view imagery in urban analytics and GIS: A review. *Landscape and Urban Planning* 215, 104217. [doi:10.1016/j.landurbplan.2021.104217](https://doi.org/10.1016/j.landurbplan.2021.104217)

Curtis, J.W., Curtis, A., Mapes, J., Szell, A.B., Cinderich, A. (2013). Using Google Street View for systematic observation of the built environment: analysis of spatio-temporal instability of imagery dates. *International Journal of Health Geographics* 12, 53. [doi:10.1186/1476-072X-12-53](https://doi.org/10.1186/1476-072X-12-53)

Fan, Z., Feng, C.-C., Biljecki, F. (2025). Coverage and bias of street view imagery in mapping the urban environment. *Computers, Environment and Urban Systems* 117, 102253. [doi:10.1016/j.compenvurbsys.2025.102253](https://doi.org/10.1016/j.compenvurbsys.2025.102253)

Hou, Y., Quintana, M., Khomiakov, M., Yap, W., Ouyang, J., Ito, K., Wang, Z., Zhao, T., Biljecki, F. (2024). Global Streetscapes — A comprehensive dataset of 10 million street-level images across 688 cities for urban science and analytics. *ISPRS Journal of Photogrammetry and Remote Sensing* 215, 216–238. [doi:10.1016/j.isprsjprs.2024.06.023](https://doi.org/10.1016/j.isprsjprs.2024.06.023)

Ito, K., Zhu, Y., Abdelrahman, M., Liang, X., Fan, Z., Hou, Y., Zhao, T., Ma, R., Fujiwara, K., Ouyang, J., Quintana, M., Biljecki, F. (2025). ZenSVI: An open-source software for the integrated acquisition, processing and analysis of street view imagery towards scalable urban science. *Computers, Environment and Urban Systems* 119, 102283. [doi:10.1016/j.compenvurbsys.2025.102283](https://doi.org/10.1016/j.compenvurbsys.2025.102283)

Mahabir, R., Schuchard, R., Crooks, A., Croitoru, A., Stefanidis, A. (2020). Crowdsourcing Street View Imagery: A Comparison of Mapillary and OpenStreetCam. *ISPRS International Journal of Geo-Information* 9(6), 341. [doi:10.3390/ijgi9060341](https://doi.org/10.3390/ijgi9060341)

Wang, Z., Ito, K., Biljecki, F. (2024). Assessing the equity and evolution of urban visual perceptual quality with time series street view imagery. *Cities* 145, 104704. [doi:10.1016/j.cities.2023.104704](https://doi.org/10.1016/j.cities.2023.104704)
