"""Generate the committed synthetic frontend e2e fixture (issue #124).

Reuses the project's OWN summarizer + aggregate code (the same functions the
real pipeline runs), so the fixture tracks the live schema — aggregate v3, the
per-run JSON, the CSV column set — instead of drifting hand-authored JSON.

Re-run after any schema/format bump, then commit the regenerated files:

    python tests/e2e/build_fixture.py

Everything lands in ``tests/e2e/fixture/`` and is tiny enough to commit:

  * ``cities.json.gz``              aggregate schema v3, 3 cities
  * ``<city_id>_..._<date>.csv.gz`` + sibling ``.json.gz`` per run
  * ``streetwalks.json.gz``         road-walk manifest (#155), 1 walk
  * ``<city_id>_..._streetwalk_sp15_<date>_coverage.json.gz``

The three cities cover the render paths the smoke test asserts on:

  * a normal multi-run **GSV** city  — snapshot ``<select>`` + change line,
    plus a road-walk coverage artifact (fractional street overlay, #155).
    It also carries a **Mapillary** run and a Mapillary road walk, so it is the
    two-provider city the pivoted grid/streets tables (#250) need: without one,
    the cross-provider Δ columns and the union-not-intersection pivot render
    nothing an assertion can see. And a second, **all_public** walk, for the
    streets page's network selector.
  * a **0-pano** GSV city (#69/#122) — "—" dates, no ``Infinity%``/``NaN``
  * a **Mapillary-only** city        — provider toggle / ``?provider=``, and
    the one tracked city with no GSV capture date, which the driving-plan join
    needs to render an ordinary "campaign closed, nothing observed" verdict

The manifest is written for every city (as in production, where it is rebuilt
with the aggregate), so a city with no walk exercises the lookup-miss path.

The catalog DB is built in a throwaway temp dir and discarded; only the gzipped
data artifacts are kept (mirrors the real publish glob, which excludes the DB).
"""

import gzip
import json
import os
import shutil
import sys
import tempfile
from datetime import date

# Make the repo root (and therefore ``tests`` and ``streetscape_metadata_tracker``)
# importable when this script is run directly.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from streetscape_metadata_tracker import analysis, db, naming  # noqa: E402
from streetscape_metadata_tracker.diff import (  # noqa: E402
    compute_run_diff,
    generate_diff_filename,
    write_diff_detail,
)
from streetscape_metadata_tracker.fileutils import load_city_csv_file  # noqa: E402
from streetscape_metadata_tracker.json_summarizer import (  # noqa: E402
    generate_aggregate_v2,
    generate_city_metadata_summary_as_json,
    generate_driving_plan_summary,
    generate_streetwalk_manifest,
)
from tests.conftest import (  # noqa: E402
    make_city_df,
    make_mapillary_city_df,
    write_city_csv_gz,
)

# Not named "data/" on purpose: the repo .gitignore excludes any data/ dir, so
# a data/ subdir here would be silently un-committable.
FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixture")

# Shared frozen grid geometry for every fixture city (small, so files stay tiny).
W = H = 100
STEP = 20


def _run_name(city_id, run_date, provider="gsv"):
    """Filename for a run's csv.gz (gsv is tokenless, per naming.py)."""
    token = "" if provider == "gsv" else f"_{provider}"
    return f"{city_id}_width_{W}_height_{H}_step_{STEP}{token}_{run_date.isoformat()}.csv.gz"


def _write_summary(csv_path, city_name, state, country, run_date, provider="gsv"):
    """Write the per-run JSON summary next to the csv; return (path, dataframe).

    The frame is handed back so the caller can run the REAL
    ``analysis.calculate_run_stats`` over it rather than hand-passing a few
    columns — see ``_register_run_with_stats``.
    """
    df = load_city_csv_file(csv_path)
    path = generate_city_metadata_summary_as_json(
        csv_path,
        df,
        city_name,
        state,
        country,
        W,
        H,
        STEP,
        force_recreate_file=True,
        run_date=run_date,
        provider=provider,
    )
    return path, df


def _register_run_with_stats(conn, *, df, run_date, provider, **kwargs):
    """Catalog a run with the stats the real pipeline would have computed.

    ``analysis.calculate_run_stats`` returns exactly ``db.register_run``'s
    stats kwargs, so passing it through is both shorter and more faithful than
    naming a few columns by hand — which is what this used to do, and which
    left ``coverage_rate_pct`` NULL on every fixture run. That is not a
    cosmetic gap: it is the aggregate's ``coverage_rate_percent``, i.e. the
    grid page's headline column, its histogram filter and (from issue #250) its
    cross-provider delta, so the e2e was asserting against a column that was
    em-dashes all the way down.

    ``num_flat_images`` stays absent on purpose: it is a downloader artifact
    (flat-only points collapse to one CSV row), not something a frame can be
    asked for.
    """
    return db.register_run(
        conn,
        run_date=run_date,
        provider=provider,
        **kwargs,
        **analysis.calculate_run_stats(df, run_date, provider),
    )


def _add_gsv_run(conn, city_id, city_name, state, country, panos, run_date, grid_origin, n_empty=1):
    name = _run_name(city_id, run_date)
    csv_path = os.path.join(FIXTURE_DIR, name)
    write_city_csv_gz(
        make_city_df(panos, run_date=run_date, grid_origin=grid_origin, n_empty=n_empty), csv_path
    )
    # Capture dates, coverage rates and status counts all come out of the run's
    # own CSV via the real stats code, so the fixture cannot drift from what
    # the pipeline would actually catalog. The driving-plan join (issue #176)
    # depends on the capture dates in particular: its whole point is imagery
    # dates contradicting Google's published windows, which is unobservable
    # without a newest_capture_date on the run.
    json_path, df = _write_summary(csv_path, city_name, state, country, run_date)
    return _register_run_with_stats(
        conn,
        df=df,
        run_date=run_date,
        provider="gsv",
        city_id=city_id,
        csv_filename=name,
        json_filename=os.path.basename(json_path),
    )


def _add_mapillary_run(conn, city_id, city_name, state, country, panos, run_date, grid_origin):
    name = _run_name(city_id, run_date, provider="mapillary")
    csv_path = os.path.join(FIXTURE_DIR, name)
    write_city_csv_gz(
        make_mapillary_city_df(panos, run_date=run_date, grid_origin=grid_origin), csv_path
    )
    json_path, df = _write_summary(csv_path, city_name, state, country, run_date, provider="mapillary")
    return _register_run_with_stats(
        conn,
        df=df,
        run_date=run_date,
        provider="mapillary",
        city_id=city_id,
        csv_filename=name,
        json_filename=os.path.basename(json_path),
    )


def _record_real_diff(conn, city_id, from_run, to_run, from_date, to_date):
    """Diff the two runs with the REAL pipeline code and publish the detail CSV.

    Computing (rather than hand-recording) the diff keeps the fixture on the
    live detail schema, and the published csv.gz is what lets the e2e exercise
    the city page's "Show changes on map" overlay end-to-end.
    """
    df_old = load_city_csv_file(os.path.join(FIXTURE_DIR, _run_name(city_id, from_date)))
    df_new = load_city_csv_file(os.path.join(FIXTURE_DIR, _run_name(city_id, to_date)))
    diff = compute_run_diff(df_old, df_new)

    detail_filename = generate_diff_filename(city_id, from_date.isoformat(), to_date.isoformat())
    write_diff_detail(diff, os.path.join(FIXTURE_DIR, detail_filename))

    db.record_diff(
        conn,
        city_id=city_id,
        from_run_id=from_run,
        to_run_id=to_run,
        grid_aligned=diff.grid_aligned,
        panos_added=diff.panos_added,
        panos_removed=diff.panos_removed,
        panos_persisted=diff.panos_persisted,
        capture_date_changed=diff.capture_date_changed,
        points_gained_coverage=diff.points_gained_coverage,
        points_lost_coverage=diff.points_lost_coverage,
        coverage_delta_pct=diff.coverage_delta_pct,
        detail_filename=detail_filename,
    )


def _add_streetwalk(
    conn,
    city_id,
    run_date,
    grid_origin,
    spacing_m=15.0,
    match_dist_m=25.0,
    provider="gsv",
    network_type="drive",
    flat_only=False,
):
    """
    Add a road-walk coverage artifact + catalog row for a city (issue #99/#155).

    Built with the REAL sampling/coverage/GeoJSON code so the fixture tracks the
    live artifact schema, from a hand-made two-edge network: one edge covered
    end-to-end and one covered only partway, so the fractional ramp has both a
    full and a partial edge to color (and the by-type chart has two classes).
    The raw sample csv.gz is deliberately NOT written — the city page never
    fetches it, and the fixture stays small.

    ``flat_only`` marks the covered samples as FLAT_ONLY instead of OK, which
    is how a Mapillary walk records street that has flat/perspective imagery
    but no 360° pano: it lifts the any-imagery number while leaving the 360°
    number at zero (issue #116's distinction). Used to give the streets page a
    second provider whose two coverage columns actually differ.

    ``network_type`` selects which OSM network the walk claims to have covered.
    The synthetic two-edge network is the same either way — what matters to the
    frontend is that the artifact, the filename token and the catalog row all
    agree, since a walk of a different network has a different street-km
    denominator and must never share a row or a column with a 'drive' one.
    """
    import geopandas as gpd
    import pandas as pd
    from shapely.geometry import LineString

    from streetscape_street_analyzer import road_sampling, street_coverage

    lat, lon = grid_origin
    edges = gpd.GeoDataFrame(
        {
            "edge_id": ["1_2", "2_3"],
            "highway": ["residential", "service"],
            "length": [222.0, 55.0],
        },
        geometry=[
            LineString([(lon, lat), (lon, lat + 0.002)]),
            LineString([(lon, lat + 0.002), (lon, lat + 0.0025)]),
        ],
        crs="EPSG:4326",
    )
    samples = road_sampling.generate_samples(edges, spacing_m=spacing_m)

    # Edge 1_2: every sample covered. Edge 2_3: only its first sample.
    def _covered(row):
        return row.edge_id == "1_2" or row.sample_idx == 0

    collected = pd.DataFrame(
        [
            {
                "query_lat": r.lat,
                "query_lon": r.lon,
                "pano_lat": r.lat if _covered(r) else None,
                "pano_lon": r.lon if _covered(r) else None,
                "pano_id": f"sw{r.Index}" if _covered(r) else None,
                "capture_date": (None if (flat_only or not _covered(r)) else "2022-06-01"),
                "copyright_info": (
                    None
                    if not _covered(r)
                    else ("© Mapillary contributor 42" if flat_only else "© Google")
                ),
                "status": (
                    "ZERO_RESULTS" if not _covered(r) else ("FLAT_ONLY" if flat_only else "OK")
                ),
                "query_timestamp": f"{run_date.isoformat()}T00:00:00Z",
            }
            for r in samples.itertuples()
        ]
    )

    # Named by the real generator so the fixture can't drift from the contract
    # (it used to hand-build the provider token, which production code did not
    # actually emit — the bug that let two providers collide on one filename).
    csv_name = (
        naming.generate_streetwalk_filename(
            city_id,
            W,
            H,
            STEP,
            spacing_m,
            run_date,
            provider=provider,
            network_type=network_type,
        )
        + ".csv.gz"
    )
    coverage_name = naming.streetwalk_coverage_filename(csv_name)

    covered = street_coverage.compute_streetwalk_coverage(
        edges, samples, collected, run_date.isoformat(), provider, match_dist_m
    )
    geojson = street_coverage.build_streetwalk_geojson(
        covered,
        city_id=city_id,
        provider=provider,
        run_date=run_date.isoformat(),
        spacing_m=spacing_m,
        match_dist_m=match_dist_m,
        source_csv=csv_name,
        network_type=network_type,
    )
    with gzip.open(os.path.join(FIXTURE_DIR, coverage_name), "wt", encoding="utf-8") as fh:
        json.dump(geojson, fh)

    totals = geojson["properties"]["metadata"]["totals"]
    db.register_street_walk(
        conn,
        city_id=city_id,
        run_date=run_date,
        csv_filename=csv_name,
        provider=provider,
        network_type=network_type,
        coverage_filename=coverage_name,
        spacing_m=spacing_m,
        match_dist_m=match_dist_m,
        sample_points=len(samples),
        edges_total=totals["edges"],
        edges_fully_covered=totals["edges_fully_covered"],
        mean_edge_coverage=totals["mean_edge_coverage"],
        coverage_pct_by_length=totals["coverage_pct_by_length"],
        coverage_pct_by_length_any=totals["coverage_pct_by_length_any"],
        coverage_by_highway=json.dumps(geojson["properties"]["metadata"]["coverage_by_highway"]),
        # Schema v12: the fixture must carry the absolute lengths and the
        # per-class breakdown, or the streets page's km columns and expandable
        # rows would render as em-dashes and the e2e assertions would pass
        # against an empty UI.
        length_km=totals["length_km"],
        length_km_covered=totals["length_km_covered"],
        length_km_covered_any=totals["length_km_covered_any"],
        median_covered_age_years=totals["median_covered_age_years"],
    )


def build():
    # Start from a clean fixture dir so re-runs are deterministic.
    if os.path.isdir(FIXTURE_DIR):
        shutil.rmtree(FIXTURE_DIR)
    os.makedirs(FIXTURE_DIR)

    # Keep the catalog DB out of the committed fixture (temp dir, discarded).
    db_tmp = tempfile.mkdtemp(prefix="gsv-e2e-db-")
    conn = db.connect(os.path.join(db_tmp, "streetscape_tracker.db"))
    try:
        # 1) Normal multi-run GSV city: two runs, a diff, a spread of years.
        alpha = db.register_city(
            conn,
            city_name="Alpha City",
            state_name="Alphastate",
            state_code="AS",
            country_name="Testland",
            country_code="TL",
            center_lat=44.00,
            center_lon=-121.00,
            grid_width_m=W,
            grid_height_m=H,
            step_m=STEP,
        )
        r1 = _add_gsv_run(
            conn,
            alpha,
            "Alpha City",
            "Alphastate",
            "Testland",
            [("a1", "2018-06-01"), ("a2", "2020-06-01")],
            date(2026, 1, 15),
            grid_origin=(44.00, -121.00),
        )
        r2 = _add_gsv_run(
            conn,
            alpha,
            "Alpha City",
            "Alphastate",
            "Testland",
            [("a1", "2018-06-01"), ("a2", "2020-06-01"), ("a3", "2024-06-01")],
            date(2026, 4, 15),
            grid_origin=(44.00, -121.00),
        )
        _record_real_diff(conn, alpha, r1, r2, date(2026, 1, 15), date(2026, 4, 15))

        # ...plus a Mapillary run of the SAME city on the SAME date. This is
        # what makes Alpha City a two-provider city, and until issue #250 the
        # fixture had none at all — so the grid page's cross-provider Δ columns,
        # the union-not-intersection pivot and the shared-geometry collapse were
        # all invisible to the e2e, which could only ever see single-provider
        # rows. Deliberately on ALPHA rather than on Map Ville (which the issue
        # suggested): the driving-plan fixture below needs one tracked city with
        # NO gsv capture date, to render the ordinary "campaign closed, nothing
        # observed" verdict, and Map Ville is it.
        _add_mapillary_run(
            conn,
            alpha,
            "Alpha City",
            "Alphastate",
            "Testland",
            [("am1", "2023-05-01"), ("am2", "2025-05-01")],
            date(2026, 4, 15),
            grid_origin=(44.00, -121.00),
        )

        # 2) 0-pano GSV city (#69/#122): a run with no panos at all.
        zero = db.register_city(
            conn,
            city_name="Zero City",
            state_name="Zerostate",
            state_code="ZS",
            country_name="Testland",
            country_code="TL",
            center_lat=45.00,
            center_lon=-120.00,
            grid_width_m=W,
            grid_height_m=H,
            step_m=STEP,
        )
        _add_gsv_run(
            conn,
            zero,
            "Zero City",
            "Zerostate",
            "Testland",
            [],
            date(2026, 4, 15),
            grid_origin=(45.00, -120.00),
            n_empty=3,
        )

        # 3) Mapillary city: one census run (drives the provider toggle).
        mapv = db.register_city(
            conn,
            city_name="Map Ville",
            state_name="Mapstate",
            state_code="MS",
            country_name="Testland",
            country_code="TL",
            center_lat=46.00,
            center_lon=-119.00,
            grid_width_m=W,
            grid_height_m=H,
            step_m=STEP,
        )
        _add_mapillary_run(
            conn,
            mapv,
            "Map Ville",
            "Mapstate",
            "Testland",
            [("m1", "2021-05-01"), ("m2", "2023-05-01")],
            date(2026, 4, 15),
            grid_origin=(46.00, -119.00),
        )

        # 4) Road-walk coverage artifacts (#155). The city page must render one
        # in place of the grid overlay, while Zero City exercises the "manifest
        # present, no entry for me" path.
        #
        # Alpha City is walked by BOTH providers on the same date and on the
        # 'drive' network, which is what gives streets.html a pivoted row with
        # two populated provider sub-columns and a non-null Δ (issue #250) — and
        # it is also the collision the provider token exists to prevent, so the
        # two artifacts must not overwrite each other.
        _add_streetwalk(conn, alpha, date(2026, 4, 15), grid_origin=(44.00, -121.00))
        _add_streetwalk(
            conn,
            alpha,
            date(2026, 4, 15),
            grid_origin=(44.00, -121.00),
            provider="mapillary",
            flat_only=True,
        )
        # ...and once more on the BROAD network, so the streets page's
        # network selector has a second series to switch to. Its street-km
        # denominator is a different one, which is exactly why it is a separate
        # row rather than another column.
        _add_streetwalk(
            conn,
            alpha,
            date(2026, 4, 15),
            grid_origin=(44.00, -121.00),
            network_type="all_public",
        )
        # A Mapillary walk on the Mapillary-only city, recorded as flat-only
        # imagery so the streets page has a row whose 360° and any-imagery
        # numbers differ.
        _add_streetwalk(
            conn,
            mapv,
            date(2026, 4, 15),
            grid_origin=(46.00, -119.00),
            provider="mapillary",
            flat_only=True,
        )

        # 5) A driving-plan snapshot to join against (issue #176), covering the
        # three cases the Driving page has to render:
        #   - Alphastate: a CLOSED 2019 window against Alpha City's 2020s GSV
        #     imagery — the driven_unplanned contradiction the page exists for,
        #     and the only verdict that needs a gsv run to observe.
        #   - Zerostate: an OPEN window, so the page also renders planned_open.
        #   - Mapstate: closed, and Map-Ville is Mapillary-only, so it has no
        #     GSV capture date to contradict the plan with — the ordinary
        #     "campaign closed, nothing observed" row.
        #   - Chubut: matches no tracked city, the collection-target case the
        #     summary below the table reports.
        # An earlier snapshot in which Alphastate's campaign was still open,
        # so the published artifact carries a real revision (Yes -> No) and the
        # page's revision log has something to render. Google overwrites the
        # feed in place, so this pair is the whole reason the archive exists.
        earlier_id = db.register_driving_plan_snapshot(
            conn,
            fetch_date=date(2026, 4, 10),
            sha256="e2efixture-earlier",
            record_count=3,
            changed=True,
            artifact_filename="gsv_driving_plan_2026-04-10.json.gz",
        )
        db.replace_driving_plan_entries(
            conn,
            earlier_id,
            [
                (
                    earlier_id,
                    "Testland",
                    "TL",
                    "SV",
                    "Alphastate",
                    "Alpha County",
                    "Yes",
                    "2019-01-01T08:00:00.000Z",
                    "2019-01-01",
                    "2019-06-01T07:00:00.000Z",
                    "2019-06-01",
                ),
                (
                    earlier_id,
                    "Testland",
                    "TL",
                    "SV",
                    "Zerostate",
                    "Zero County",
                    "Yes",
                    "2026-03-01T08:00:00.000Z",
                    "2026-03-01",
                    "2026-11-01T07:00:00.000Z",
                    "2026-11-01",
                ),
                (
                    earlier_id,
                    "Testland",
                    "TL",
                    "SV",
                    "Mapstate",
                    "Map County",
                    "No",
                    "2019-01-01T08:00:00.000Z",
                    "2019-01-01",
                    "2019-06-01T07:00:00.000Z",
                    "2019-06-01",
                ),
                (
                    earlier_id,
                    "Argentina",
                    "AR",
                    "SV",
                    "Chubut",
                    "Esquel",
                    "Yes",
                    "2026-01-01T08:00:00.000Z",
                    "2026-01-01",
                    "2026-12-31T08:00:00.000Z",
                    "2026-12-31",
                ),
            ],
        )

        snapshot_id = db.register_driving_plan_snapshot(
            conn,
            fetch_date=date(2026, 4, 20),
            sha256="e2efixture",
            record_count=3,
            changed=True,
            artifact_filename="gsv_driving_plan_2026-04-20.json.gz",
        )
        db.replace_driving_plan_entries(
            conn,
            snapshot_id,
            [
                # (snapshot, country, code, svspc, region, district, publish,
                #  start_raw, start, end_raw, end) — raw precedes parsed.
                (
                    snapshot_id,
                    "Testland",
                    "TL",
                    "SV",
                    "Alphastate",
                    "Alpha County",
                    "No",
                    "2019-01-01T08:00:00.000Z",
                    "2019-01-01",
                    "2019-06-01T07:00:00.000Z",
                    "2019-06-01",
                ),
                (
                    snapshot_id,
                    "Testland",
                    "TL",
                    "SV",
                    "Zerostate",
                    "Zero County",
                    "Yes",
                    "2026-03-01T08:00:00.000Z",
                    "2026-03-01",
                    "2026-11-01T07:00:00.000Z",
                    "2026-11-01",
                ),
                (
                    snapshot_id,
                    "Testland",
                    "TL",
                    "SV",
                    "Mapstate",
                    "Map County",
                    "No",
                    "2019-01-01T08:00:00.000Z",
                    "2019-01-01",
                    "2019-06-01T07:00:00.000Z",
                    "2019-06-01",
                ),
                (
                    snapshot_id,
                    "Argentina",
                    "AR",
                    "SV",
                    "Chubut",
                    "Esquel",
                    "Yes",
                    "2026-01-01T08:00:00.000Z",
                    "2026-01-01",
                    "2026-12-31T08:00:00.000Z",
                    "2026-12-31",
                ),
            ],
        )

        # 6) Aggregate → cities.json.gz (schema v3), the streetwalk sidecar
        # manifest and the driving-plan join, all written into FIXTURE_DIR (as
        # the real pipeline does).
        summary = generate_aggregate_v2(conn, FIXTURE_DIR)
        generate_streetwalk_manifest(conn, FIXTURE_DIR)
        generate_driving_plan_summary(conn, FIXTURE_DIR)
    finally:
        conn.close()
        shutil.rmtree(db_tmp, ignore_errors=True)

    files = sorted(os.listdir(FIXTURE_DIR))
    print(f"Wrote {len(files)} files to {FIXTURE_DIR} ({summary['cities_count']} cities):")
    for f in files:
        print(f"  {f}")
    return FIXTURE_DIR


if __name__ == "__main__":
    build()
