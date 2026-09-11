# Frontend e2e smoke test (issue #124)

Automates the manual browser verification (#92 task 2) as a CI job so
full-render regressions in `www/` are caught automatically. Real headless
Chromium is driven over a small **committed synthetic fixture**, with the
frontend's data fetches intercepted so no live data host is needed.

## Files

| File | Purpose |
| --- | --- |
| `build_fixture.py` | Regenerates the committed fixture using the project's own summarizer + aggregate code. |
| `fixture/` | Committed synthetic data: `cities.json.gz` (aggregate v3) + per-run `csv.gz`/`json.gz`. |
| `test_smoke.py` | Serves `www/`, intercepts `**/streetscape-tracker/data/**` with the fixture, drives Chromium. |

The fixture has three cities, one per render path the test asserts on:

- **Alpha City** — a normal multi-run GSV city (snapshot `<select>` + change line), also collected
  and walked by Mapillary and Panoramax. The THREE-provider city: the widest row the pivoted
  tables render, and the count at which the default preset stops fitting the measure (#334).
- **Zero City** — a 0-pano GSV city (#69/#122: `—` dates, no `Infinity%`/`NaN`)
- **Map Ville** — a Mapillary city (provider toggle / `?provider=`), excluded from both GSV channels

## Running locally

```bash
source .venv/bin/activate
pip install pytest-playwright          # not in requirements-dev; e2e-only
python -m playwright install chromium  # one-time browser download
pytest tests/e2e -m e2e                # run the smoke test
```

These tests are marked `e2e` and **excluded from the default `pytest` run** (see
`addopts` in `pyproject.toml`); the fast suite stays pure-logic and offline. The
module also `importorskip`s `pytest_playwright`, so a plain `pytest` without the
plugin installed simply skips it.

## Regenerating the fixture

Re-run after any schema/format change (aggregate v3, per-run JSON, CSV columns),
then commit the regenerated `fixture/` files:

```bash
python tests/e2e/build_fixture.py
```

Because the fixture is produced by the same functions the real pipeline runs
(`generate_city_metadata_summary_as_json`, `generate_aggregate_v2`), it tracks
the live schema instead of drifting as hand-authored JSON would.

## How data is served

The frontend hardcodes the production data host (`STREETSCAPE_DATA_BASE_URL`). Rather
than change prod code, the test intercepts requests to `**/streetscape-tracker/data/**`
and fulfills them with the fixture bytes **raw** (no `Content-Encoding: gzip`),
so the page's own `pako` / `DecompressionStream("gzip")` decompresses exactly as
in production.

## Notes

- The page still loads Leaflet / Chart.js / etc. from their CDNs, so the e2e job
  needs network (GitHub runners have it); it is not fully offline like the unit
  tests. The CI job is **non-blocking** (`continue-on-error: true`).
- Third-party console noise (analytics, favicon) is filtered so the
  "console clean" assertion tracks our own code.
