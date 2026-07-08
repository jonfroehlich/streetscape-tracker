# data_sources/

Reference datasets used to build the **worldwide city-sampling frame**
(`scripts/build_worldwide_frame.py`). These are inputs to a one-off (rarely
re-run) selection step; they are **not** collected imagery data and are **not**
published to the web server. (`data/` is rsynced and git-ignored;
`data_sources/` is neither.)

## Files

| File | Source | What it provides |
|------|--------|------------------|
| `cities15000.txt` | GeoNames | All cities with population > 15,000 (~34k rows). Columns per the GeoNames "geoname" schema (tab-separated, no header). We use: `asciiname`, `country code` (ISO-3166 alpha-2), `admin1 code`, `population`, `latitude`, `longitude`. |
| `countryInfo.txt` | GeoNames | ISO-2 → country name and continent code (`AF/AS/EU/NA/SA/OC/AN`). Leading `#` comment lines describe the columns. |
| `admin1CodesASCII.txt` | GeoNames | `"<iso2>.<admin1code>"` → admin-1 (state/province/region) name, for building human-readable geocoding queries. |
| `gsv_coverage_regime.csv` | Hand-maintained (this repo) | Per-country GSV coverage regime (`present`/`sparse`/`absent`). Force-includes the cross-provider (GSV-absent, Mapillary-present) story into the frame. Edit as coverage changes. |

## Attribution / license

GeoNames data (`cities15000.txt`, `countryInfo.txt`, `admin1CodesASCII.txt`) is
© GeoNames, licensed under **Creative Commons Attribution 4.0**
(<https://creativecommons.org/licenses/by/4.0/>). Source:
<https://download.geonames.org/export/dump/>.

## Refreshing

The frame is deterministic given these inputs, so we vendor them for
reproducibility rather than downloading at build time. To refresh:

```bash
cd data_sources
curl -sSLO https://download.geonames.org/export/dump/cities15000.zip && unzip -o cities15000.zip && rm cities15000.zip
curl -sSLO https://download.geonames.org/export/dump/countryInfo.txt
curl -sSLO https://download.geonames.org/export/dump/admin1CodesASCII.txt
```

Then re-run `python scripts/build_worldwide_frame.py` and review the diff to
`worldwide_frame.csv` before re-registering any cities.
