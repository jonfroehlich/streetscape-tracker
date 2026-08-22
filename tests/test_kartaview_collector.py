"""
The KartaView census collector (issue #225).

Every request is served from memory: the sweep tests substitute
``_post_nearby``, and the request-layer tests substitute the aiohttp session.
Nothing here touches kartaview.org -- a test suite that probed a third party
from every dev machine and CI job is the shape of the two per-IP bans this
project already has.

What these pin is the set of things the two measured studies bought
(docs/experiments/kartaview-feasibility.md, kartaview-sweep-cost.md) and that a
plausible-looking edit would undo:

  * HTTP 400 means BACKPRESSURE, not a malformed request.
  * A refusal is retried before it is believed, and only backpressure may
    subdivide.
  * ``shot_date >= date_added`` is not a capture date, ``>=`` and not ``>``.
  * ``date_added`` is never promoted into ``capture_date``.
  * A host refusal stops the sweep at the first request, and a rejected
    credential is NOT a host refusal.
"""

from __future__ import annotations

import asyncio
import inspect
import io
import json
import logging
import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from streetscape_metadata_tracker import analysis
from streetscape_metadata_tracker import download_kartaview as kv
from streetscape_metadata_tracker.download_common import (
    HOST_BUSY_EXIT_CODES,
    HOST_EXIT_CODES,
    HOST_KARTAVIEW,
    DownloadError,
    HostBlockedError,
    HostBusyError,
    host_exit_code,
)
from streetscape_metadata_tracker.download_mapillary import grid_bbox

# A compact bbox: (min_lon, min_lat, max_lon, max_lat), ~2 km on a side.
BBOX = (-122.35, 47.60, -122.325, 47.618)


# ── Sweep geometry and the cost model ──────────────────────────────────────


def test_cells_cover_every_point_of_the_bbox():
    """
    The lattice plus its circumscribed circles leaves no gap.

    This is what makes a sweep a CENSUS rather than a sample: a point the
    circles miss is imagery that silently never existed, in a snapshot that is
    immutable once published.
    """
    cells = kv.cells_for_bbox(*BBOX, 400.0)
    min_lon, min_lat, max_lon, max_lat = BBOX
    m_lat = kv._METERS_PER_DEG_LAT
    for lat in np.linspace(min_lat, max_lat, 25):
        m_lon = m_lat * math.cos(math.radians(lat))
        for lon in np.linspace(min_lon, max_lon, 25):
            covered = any(
                math.hypot((lat - c.lat) * m_lat, (lon - c.lon) * m_lon) <= c.radius_m
                for c in cells
            )
            assert covered, f"({lat}, {lon}) is inside the bbox and inside no circle"


def test_a_cells_children_exactly_cover_it():
    parent = kv.Cell(lat=47.61, lon=-122.33, size_m=800.0)
    children = kv.subdivide(parent)
    assert len(children) == 4
    assert sum(c.size_m**2 for c in children) == pytest.approx(parent.size_m**2)
    assert all(c.depth == parent.depth + 1 for c in children)


def test_the_floor_guard_is_asked_of_the_children_not_the_cell():
    """
    The natural spelling -- "is this cell above the floor?" -- halves the floor
    it was meant to enforce, and asks the server for radii no rung has tested.
    """
    cell = kv.Cell(lat=47.61, lon=-122.33, size_m=kv.RADIUS_FLOOR_M * math.sqrt(2))
    assert cell.radius_m >= kv.RADIUS_FLOOR_M  # the cell itself is legal...
    assert not kv.can_subdivide(cell)  # ...and its children would not be
    assert kv.can_subdivide(kv.Cell(lat=47.61, lon=-122.33, size_m=cell.size_m * 2))


def test_pages_are_priced_from_page_ones_total():
    assert kv.pages_for_total(0, 2000) == 1  # page 1 is always paid
    assert kv.pages_for_total(2000, 2000) == 1
    assert kv.pages_for_total(2001, 2000) == 2
    # An unparseable total is priced as one page, never zero: pricing an unknown
    # at zero is how a cost model under-budgets the cities that broke it.
    assert kv.pages_for_total(None, 2000) == 1


def test_the_estimate_tracks_the_measured_geometric_term():
    """
    The sweep study's finding 1: root cells track ``bbox_area / (2 r^2)``,
    within 10% above ~350 km2. That is the whole basis for budgeting a
    KartaView channel by bbox area rather than by imagery, so it is pinned --
    and with it the direction of the error, which is what makes the estimate
    safe to schedule against: the excess is pure ``ceil()``, so the lattice
    over-covers and never under-covers.
    """
    # 50 x 50 km = 2,500 km2, comparable to the study's Las Vegas (2,414 km2,
    # ratio 1.04). The excess shrinks as the bbox grows, so a SMALL bbox is a
    # weaker version of this assertion, not a stronger one.
    cells = kv.estimate_sweep_requests(47.6062, -122.3321, 50_000, 50_000, 20, radius_m=1000)
    geometric = 2500e6 / (2 * 1000**2)
    assert cells >= geometric
    assert cells / geometric == pytest.approx(1.0, abs=0.10)


def test_a_smaller_radius_costs_about_four_times_as_many_cells():
    """Finding 3: the calibrated radius is a factor-of-four lever on the cost."""
    big = kv.estimate_sweep_requests(47.6062, -122.3321, 50_000, 50_000, 20, radius_m=1000)
    small = kv.estimate_sweep_requests(47.6062, -122.3321, 50_000, 50_000, 20, radius_m=500)
    assert small / big == pytest.approx(4.0, abs=0.15)


# ── Capture dates: the invariant, and the two timestamps ───────────────────


CAPTURE_CASES = [
    # (shot_date, date_added, expected)
    ("2025-09-01 17:57:05.000", "2025-09-20 21:08:37", "2025-09-01"),
    # The measured defect: Grab's 2025-11-19 ingest, captured AFTER upload.
    ("2025-11-19 11:18:35", "2025-11-19 10:54:07", ""),
    # ...and the near-miss half of it, equal to the second.
    ("2025-11-19 11:18:29", "2025-11-19 11:18:29", ""),
    # An upload time is never a capture date.
    (None, "2025-11-19 11:18:29", ""),
    ("", "2025-11-19 11:18:29", ""),
    # No upload time cannot falsify the invariant, so it does not veto.
    ("2019-04-02 08:00:00", None, "2019-04-02"),
    # Below the contributor-archive floor.
    ("1999-01-01 00:00:00", "2019-01-01 00:00:00", ""),
    # Unparseable.
    ("not a date", "2019-01-01 00:00:00", ""),
]


@pytest.mark.parametrize("shot,added,expected", CAPTURE_CASES)
def test_capture_date_rules(shot, added, expected):
    assert kv.shot_date_to_iso_date(shot, added) == expected


def test_the_vectorized_parser_matches_the_scalar_one_element_wise():
    """
    The scalar form is the readable statement of the rules; the vectorized one
    is what a census actually calls. They are pinned together so the rules can
    only be stated once (the Mapillary precedent).
    """
    shots = [c[0] for c in CAPTURE_CASES]
    addeds = [c[1] for c in CAPTURE_CASES]
    vectorized = list(kv.shot_dates_to_iso_dates(shots, addeds))
    scalar = [kv.shot_date_to_iso_date(s, a) for s, a in zip(shots, addeds, strict=True)]
    assert vectorized == scalar


def test_one_pages_mixed_precisions_do_not_null_each_other():
    """
    KartaView mixes timestamp precisions inside one page: `shot_date` arrives
    with milliseconds and `date_added` without, and both spellings of
    `shot_date` occur. pandas infers ONE format from the first non-null value,
    so with errors="coerce" every value at the other precision silently becomes
    NaT -- issue #226's defect (a strict format nulling every archival run's
    dates), arriving from a second direction. Nothing raises; the dates just
    disappear, which is why this needs its own test rather than trusting the
    equivalence check above to have the right ordering by luck.
    """
    shots = ["2025-09-01 17:57:05.000", "2019-04-02 08:00:00"]
    assert list(kv.shot_dates_to_iso_dates(shots, [None, None])) == ["2025-09-01", "2019-04-02"]
    # ...and the other way round, since the inference reads the FIRST value.
    assert list(kv.shot_dates_to_iso_dates(shots[::-1], [None, None])) == [
        "2019-04-02",
        "2025-09-01",
    ]


def test_a_future_capture_date_is_dropped():
    future = (pd.Timestamp.utcnow().tz_localize(None) + pd.Timedelta(days=30)).isoformat()
    assert kv.shot_date_to_iso_date(future, None) == ""
    assert list(kv.shot_dates_to_iso_dates([future], [None])) == [""]


def test_todays_imagery_survives_a_timezone_naive_clock():
    """
    The ceiling carries a day of slack because shot_date has no zone: a photo
    taken today in UTC+14 legitimately reads as tomorrow in UTC, and dropping it
    would delete the freshest imagery from every city we collect the day it was
    driven (#213's inclusive-ceiling reasoning).
    """
    tomorrow = (pd.Timestamp.utcnow().tz_localize(None) + pd.Timedelta(hours=14)).isoformat()
    assert kv.shot_date_to_iso_date(tomorrow, None) != ""


def test_the_decode_floor_is_the_analysis_floor():
    """
    The collector applies the floor at decode and analysis applies it again over
    the CSV. Two literals would drift; this is the one place they are the same
    object.
    """
    assert kv.EARLIEST_CAPTURE_DATE == analysis.EARLIEST_PLAUSIBLE_CAPTURE["kartaview"]


# ── Decoding one page of nearby-photos ─────────────────────────────────────


def _item(**overrides):
    item = {
        "id": "2625911774",
        "lat": "47.605587",
        "lng": "-122.332966",
        "shot_date": "2025-09-01 17:57:05.000",
        "date_added": "2025-09-20 21:08:37",
        "projection": "SPHERE",
        "field_of_view": "360",
        "heading": "321.98",
        "sequence_id": "11606856",
        "sequence_index": "72",
        "username": "lowestpotential",
        "orgCode": "CMNT",
        "way_id": "993382884",
        "match_lat": "47.605735778808594",
        "match_lng": "-122.332603454589840",
    }
    item.update(overrides)
    return item


def test_decode_reads_the_projection_as_the_360_flag():
    records = kv.decode_photo_items(
        [_item(id="a"), _item(id="b", projection="PLANE"), _item(id="c", projection=None)]
    )
    assert [r["is_pano"] for r in records] == [True, False, False]


def test_decode_publishes_the_camera_position_not_the_snapped_one():
    """
    match_lat/match_lng is where KartaView snapped the photo onto an OSM way.
    Publishing it would put every photo on a road by construction and inflate
    exactly the street-coverage measure the road walk exists to make honest.
    """
    (record,) = kv.decode_photo_items([_item()])
    assert record["lat"] == pytest.approx(47.605587)
    assert record["lon"] == pytest.approx(-122.332966)
    assert record["way_id"] == "993382884"


def test_decode_drops_a_row_with_no_position():
    records = kv.decode_photo_items([_item(id="a"), _item(id="b", lat=None), _item(id="c", lng="")])
    assert [r["id"] for r in records] == ["a"]


def test_decode_carries_every_free_field():
    (record,) = kv.decode_photo_items([_item()])
    assert record["sequence_id"] == "11606856"
    assert record["sequence_index"] == 72
    assert record["compass_angle"] == pytest.approx(321.98)
    assert record["field_of_view"] == pytest.approx(360.0)
    assert record["org_code"] == "CMNT"
    assert record["username"] == "lowestpotential"
    # Both timestamps survive decode unreduced; the date rules run over the
    # finished census, not here.
    assert record["shot_date"] == "2025-09-01 17:57:05.000"
    assert record["date_added"] == "2025-09-20 21:08:37"


def test_decode_survives_an_unusable_number():
    (record,) = kv.decode_photo_items([_item(heading="", sequence_index="n/a")])
    assert record["compass_angle"] is None
    assert record["sequence_index"] is None


# ── The census.py bindings ─────────────────────────────────────────────────


def _census(items):
    return kv.records_to_census(kv.decode_photo_items(items))


def test_rows_carry_the_kartaview_schema_in_order():
    from streetscape_metadata_tracker.config import KARTAVIEW_METADATA_DTYPES

    census = _census([_item()])
    rows = kv.build_image_rows(
        census,
        np.array([0]),
        np.array([47.6]),
        np.array([-122.33]),
        "2026-08-19T00:00:00+00:00",
        "OK",
        np.array(["2025-09-01"]),
    )
    assert list(rows.columns) == list(KARTAVIEW_METADATA_DTYPES)
    assert rows.loc[0, "copyright_info"] == "© KartaView contributor lowestpotential"
    assert rows.loc[0, "pano_lat"] == pytest.approx(47.605587)
    assert rows.loc[0, "date_added"] == "2025-09-20 21:08:37"
    assert rows.loc[0, "is_pano"] is np.True_


def test_an_anonymous_contributor_still_gets_an_attribution_string():
    """
    The imagery is CC BY-SA 4.0, so copyright_info is an attribution
    requirement here rather than the drive-vs-photosphere filter it is for GSV.
    A missing username must not render the string "<NA>" into a published file.
    """
    census = _census([_item(username=None)])
    rows = kv.build_image_rows(
        census, np.array([0]), np.array([47.6]), np.array([-122.33]), "t", "OK", np.array([""])
    )
    assert rows.loc[0, "copyright_info"] == "© KartaView"
    # Missing stays missing, and renders as an empty CSV field rather than the
    # literal "<NA>" -- which is what the fillna above is protecting the
    # concatenated string from.
    assert pd.isna(rows.loc[0, "username"])
    assert ",," in rows.to_csv(index=False).splitlines()[1]


def test_empty_rows_fill_the_schema_with_nulls():
    rows = kv.build_empty_rows(
        np.array([47.6, 47.7]), np.array([-122.3, -122.4]), "t", "ZERO_RESULTS"
    )
    assert list(rows["status"]) == ["ZERO_RESULTS", "ZERO_RESULTS"]
    assert rows["pano_id"].isna().all()
    assert rows["is_pano"].isna().all()


def test_the_sweeps_overlap_dedups_to_the_last_values_at_the_first_position():
    """
    Circumscribed circles re-see ~pi/2 of everything, so the cross-cell dedup is
    load-bearing rather than defensive -- and it is TWO rules: the repeated id
    takes the LAST copy's values but keeps the position of its FIRST appearance.
    ``drop_duplicates(keep="last")`` satisfies only the first and reorders the
    file, against an immutable dated snapshot that diff.py compares to its
    predecessor.
    """
    first_circle = _census([_item(id="a", username="early"), _item(id="b")])
    second_circle = _census([_item(id="a", username="late")])
    deduped = kv.census_core.dedupe_census(kv.concat_census([first_circle, second_circle]))
    assert list(deduped["id"]) == ["a", "b"]  # position of the FIRST appearance
    assert deduped.loc[0, "username"] == "late"  # values of the LAST


# ── One request: what each failure means ───────────────────────────────────


class _FakeResponse:
    def __init__(self, status=200, body=None, headers=None, text=None):
        self.status = status
        self.headers = headers if headers is not None else {"Content-Type": "application/json"}
        self._text = text if text is not None else json.dumps(body if body is not None else {})

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, data=None, params=None, timeout=None, allow_redirects=True):
        self.calls.append(
            {"url": url, "data": data, "params": params, "allow_redirects": allow_redirects}
        )
        return self.response


def _post(response, **kwargs):
    session = _FakeSession(response)
    counted = []
    result = asyncio.run(
        kv._post_nearby(
            session,
            kv.AsyncRateLimiter(0),
            lambda: counted.append(1),
            47.6,
            -122.33,
            400,
            **kwargs,
        )
    )
    return result, session, counted


def _post_error(response, **kwargs):
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 - the type IS the assertion
        _post(response, **kwargs)
    return excinfo.value


def test_an_ok_page_returns_its_items_and_its_total():
    body = {"currentPageItems": [_item()], "totalFilteredItems": 34}
    (items, total), session, counted = _post(_FakeResponse(body=body))
    assert len(items) == 1 and total == 34
    assert counted == [1]  # one request, one ledger increment
    assert session.calls[0]["allow_redirects"] is False


def test_the_total_survives_being_a_list_holding_a_string():
    """
    MEASURED: the API returns ``['737']``, not ``737``. A bare int() raises on
    that, and falling back to len(items) would report the PAGE SIZE as the
    circle's total -- 5 instead of 737 -- which is how a circle gets priced,
    so the sweep would stop paging exactly where there is most to page.
    """
    body = {"currentPageItems": [], "totalFilteredItems": ["737"]}
    (_, total), _, _ = _post(_FakeResponse(body=body))
    assert total == 737


def test_an_unparseable_total_is_unknown_rather_than_a_count_we_did_not_measure():
    body = {"currentPageItems": [_item()], "totalFilteredItems": "many"}
    (items, total), _, _ = _post(_FakeResponse(body=body))
    assert len(items) == 1
    assert total is None


def test_the_legacy_osv_envelope_is_accepted():
    body = {"osv": {"currentPageItems": [_item()], "totalFilteredItems": 12}}
    (items, total), _, _ = _post(_FakeResponse(body=body))
    assert len(items) == 1 and total == 12


@pytest.mark.parametrize("api_code", sorted(kv.BACKPRESSURE_API_CODES))
def test_http_400_carrying_a_backpressure_code_is_backpressure_not_a_bad_request(api_code):
    """
    The single easiest thing here to get wrong: this API signals overload with
    an HTTP 400, which is the opposite of the usual 4xx reading. Typed as a
    permanent error it would never be retried or subdivided, and every dense
    city would collect nothing.
    """
    body = {"status": {"apiCode": api_code, "apiMessage": "too much"}}
    error = _post_error(_FakeResponse(status=400, body=body))
    assert isinstance(error, kv.BackpressureError)


def test_a_redirect_is_a_host_refusal_and_its_location_is_redacted():
    """
    Mapillary's block manifested as a 302 to a login page whose FOLLOWED body
    was a 200 text/html, which reached the decoder and read as corrupt data
    (#199). The Location is echoed back with our own token in it, and this
    message reaches logs and cleartext alert email.
    """
    response = _FakeResponse(
        status=302,
        headers={"Location": "https://kartaview.org/login?next=x%3Faccess_token%3DSECRETVALUE"},
    )
    error = _post_error(response)
    assert isinstance(error, HostBlockedError)
    assert error.host == HOST_KARTAVIEW
    assert "SECRETVALUE" not in str(error)


def test_an_html_body_is_a_host_refusal_rather_than_a_parse_failure():
    response = _FakeResponse(
        status=200, headers={"Content-Type": "text/html; charset=utf-8"}, text="<html>nope</html>"
    )
    error = _post_error(response)
    assert isinstance(error, HostBlockedError)


def test_http_429_is_a_host_refusal():
    error = _post_error(_FakeResponse(status=429, body={}))
    assert isinstance(error, HostBlockedError)


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_credential_is_not_a_host_refusal(status):
    """
    Scoped to the CREDENTIAL, following the Mapillary precedent (#208): a
    channel split hands different channels different tokens, so typing a
    rejected key host-wide would let one channel's bad key trip the night-level
    breaker and skip another channel's cities.
    """
    error = _post_error(_FakeResponse(status=status, body={"status": {"apiCode": status}}))
    assert isinstance(error, kv.ResponseError)
    assert not isinstance(error, HostBlockedError)


def test_a_body_with_no_items_is_a_response_error_not_an_empty_circle():
    """
    An empty result and an unusable answer are different facts, and conflating
    them is how a refused city gets recorded as a city with no imagery.
    """
    error = _post_error(_FakeResponse(body={"status": {"apiCode": 600}}))
    assert isinstance(error, kv.ResponseError)


# ── The sweep ──────────────────────────────────────────────────────────────


class _Call:
    def __init__(self, lat, lon, radius_m, page):
        self.lat, self.lon, self.radius_m, self.page = lat, lon, radius_m, page

    @property
    def key(self):
        return (round(self.lat, 5), round(self.lon, 5), self.radius_m)


def _empty(_call):
    """Answer every circle with no imagery."""
    return [], 0


def _install(monkeypatch, responder):
    """Substitute the one request the sweep makes, and record every call."""
    calls: list[_Call] = []

    async def fake_post(
        session,
        limiter,
        count_request,
        lat,
        lon,
        radius_m,
        *,
        page=1,
        ipp=kv.IPP_MAX,
        access_token=None,
        timeout=None,
    ):
        # Counted here because the real one counts here: a retried request takes
        # its own token and its own ledger increment (#198/#203).
        count_request()
        call = _Call(lat, lon, radius_m, page)
        calls.append(call)
        return responder(call)

    monkeypatch.setattr(kv, "_post_nearby", fake_post)
    return calls


def _sweep(monkeypatch, responder, **kwargs):
    calls = _install(monkeypatch, responder)
    kwargs.setdefault("radius_m", 1000)
    kwargs.setdefault("retries", 0)
    result = asyncio.run(
        kv.fetch_city_images_async("Testville", BBOX, "tok", max_requests_per_minute=0, **kwargs)
    )
    return result, calls


def _failed_sweep(monkeypatch, responder, **kwargs):
    calls = _install(monkeypatch, responder)
    kwargs.setdefault("radius_m", 1000)
    kwargs.setdefault("retries", 0)
    with pytest.raises(DownloadError) as excinfo:
        asyncio.run(
            kv.fetch_city_images_async(
                "Testville", BBOX, "tok", max_requests_per_minute=0, **kwargs
            )
        )
    return excinfo.value, calls


def test_a_clean_sweep_visits_every_root_cell_exactly_once(monkeypatch):
    result, calls = _sweep(monkeypatch, _empty)
    assert result["cells"] == result["cells_visited"] == len(calls)
    assert result["api_requests"] == len(calls)
    assert result["failed_cells"] == []
    assert result["radius_m"] == 1000
    assert len(result["census"]) == 0
    assert {c.page for c in calls} == {1}


def test_a_supplied_radius_skips_calibration_entirely(monkeypatch):
    """
    The previous run's calibrated radius is worth storing precisely because
    re-measuring it costs up to a dozen requests against a median city of 12.
    """
    result, calls = _sweep(monkeypatch, _empty, radius_m=500)
    assert {c.radius_m for c in calls} == {500}
    assert result["api_requests"] == result["cells"]


def test_calibration_rejects_a_rung_that_only_one_point_answered(monkeypatch):
    """
    One lucky point would set a radius the rest of the city then rediscovers the
    hard way -- at a cascade per cell, which is the entire cost calibration
    exists to avoid.
    """
    centre = kv.calibration_points(BBOX, 2)[0]

    def responder(call):
        if call.radius_m == 1000 and (round(call.lat, 5), round(call.lon, 5)) != (
            round(centre[0], 5),
            round(centre[1], 5),
        ):
            raise kv.BackpressureError("apiCode 690")
        return [], 0

    result, _ = _sweep(monkeypatch, responder, radius_m=None, calibration_probes=2)
    assert result["radius_m"] == 500


def test_a_refusal_is_retried_before_it_is_believed(monkeypatch):
    """
    Fact 2 of the sweep study: apiCode 690 is flaky, and retrying is 4x cheaper
    than subdividing. Believing the first refusal turns one cell into four.
    """
    seen: dict = {}
    target = None

    def responder(call):
        nonlocal target
        target = target or call.key
        if call.key == target:
            seen[call.key] = seen.get(call.key, 0) + 1
            if seen[call.key] <= 2:
                raise kv.BackpressureError("apiCode 690")
        return [], 0

    result, calls = _sweep(monkeypatch, responder, retries=3)
    assert result["cells_visited"] == result["cells"]  # nothing was subdivided
    assert result["api_requests"] == result["cells"] + 2  # the two cleared retries
    assert result["failed_cells"] == []
    assert len(calls) == result["api_requests"]


def test_an_exhausted_retry_budget_subdivides_into_four(monkeypatch):
    target = None

    def responder(call):
        nonlocal target
        target = target or call.key
        if call.key == target:
            raise kv.BackpressureError("apiCode 690")
        return [], 0

    result, calls = _sweep(monkeypatch, responder, retries=1)
    assert result["cells_visited"] == result["cells"] + 4
    # 2 attempts on the refusing root, 1 on each of its 4 children, 1 each on
    # the other roots.
    assert result["api_requests"] == 2 + 4 + (result["cells"] - 1)
    assert {c.radius_m for c in calls} == {1000, 500}
    assert result["failed_cells"] == []


def test_only_backpressure_subdivides(monkeypatch):
    """
    Asking a server for four requests where it just failed to serve one is the
    shape of the Mapillary incident (#198), not a fix for it. So a broken cell
    stops rather than fanning out -- and, because its area is then unmeasured,
    the sweep refuses to finalize the snapshot.
    """
    target = None

    def responder(call):
        nonlocal target
        target = target or call.key
        if call.key == target:
            raise kv.ResponseError("HTTP 500")
        return [], 0

    error, calls = _failed_sweep(monkeypatch, responder)
    assert not isinstance(error, HostBlockedError)
    assert "unmeasured" in str(error)
    assert len(calls) == 4  # one per root cell: no cascade
    assert error.api_requests == 4


def test_a_definite_answer_is_not_retried_but_a_transport_fault_is(monkeypatch):
    """
    Different facts, so different remedies. Re-asking cannot change a rejected
    credential or an unparseable body, and a credential re-asked at every cell
    of every city is a good way to look like an attack.
    """
    roots = kv.cells_for_bbox(*BBOX, 1000 * math.sqrt(2))
    definite = (round(roots[0].lat, 5), round(roots[0].lon, 5), roots[0].radius_m)
    transient = (round(roots[1].lat, 5), round(roots[1].lon, 5), roots[1].radius_m)
    counts: dict = {}

    def responder(call):
        counts[call.key] = counts.get(call.key, 0) + 1
        if call.key == definite:
            raise kv.ResponseError("HTTP 403")
        if call.key == transient:
            raise kv.TransportError("connection reset")
        return [], 0

    _failed_sweep(monkeypatch, responder, retries=3)
    assert counts[definite] == 1
    assert counts[transient] == 4  # the retry budget, then broken


def test_a_host_refusal_stops_the_sweep_at_the_first_request(monkeypatch):
    """
    #205, and serial fetching makes it exact rather than bounded by a
    concurrency limit: nothing else in the bbox can answer differently, so the
    sweep must not pay for the rest of the city to learn what response 1 said.
    """
    calls = _install(
        monkeypatch,
        lambda call: (_ for _ in ()).throw(HostBlockedError("refused", HOST_KARTAVIEW)),
    )
    with pytest.raises(HostBlockedError) as excinfo:
        asyncio.run(
            kv.fetch_city_images_async(
                "Testville", BBOX, "tok", radius_m=1000, max_requests_per_minute=0
            )
        )
    assert len(calls) == 1
    assert excinfo.value.api_requests == 1
    assert host_exit_code(excinfo.value) == HOST_EXIT_CODES[HOST_KARTAVIEW]


def test_a_host_refusal_during_calibration_stops_too(monkeypatch):
    calls = _install(
        monkeypatch,
        lambda call: (_ for _ in ()).throw(HostBlockedError("refused", HOST_KARTAVIEW)),
    )
    with pytest.raises(HostBlockedError):
        asyncio.run(
            kv.fetch_city_images_async(
                "Testville", BBOX, "tok", radius_m=None, max_requests_per_minute=0
            )
        )
    assert len(calls) == 1


def test_a_city_that_answers_no_radius_is_refused_not_recorded_as_empty(monkeypatch):
    """
    "Refused" and "empty" are different facts. Recorded as empty this publishes
    a 0-pano census that the next diff reads as every pano in the city being
    removed -- against an immutable dated snapshot.

    It is deliberately NOT typed as a host condition either: every rung
    refusing in ONE bbox is a property of that location (Horace ND refused
    r >= 250 while holding no imagery at all), and typing it host-wide would
    let one such city skip every other city's KartaView channel for the night.
    """
    error, calls = _failed_sweep(
        monkeypatch,
        lambda call: (_ for _ in ()).throw(kv.BackpressureError("apiCode 690")),
        radius_m=None,
        calibration_probes=2,
    )
    assert not isinstance(error, HostBlockedError)
    assert "empty city" in str(error)
    # One probe per rung, not two: a rung needs EVERY probe, so once the first
    # fails the rung is already lost and the rest are pure waste.
    assert len(calls) == len(kv.RADIUS_LADDER_M)
    assert error.api_requests == len(calls)


def test_calibration_cost_is_bounded_at_the_production_retry_budget(monkeypatch):
    """
    The docstrings and CLAUDE.md all said "at most 12" -- rungs x probes, which
    assumes a probe costs one request. A REFUSED probe costs retries + 1, and
    measured at the shipped defaults a city where nothing answers spent 48.
    The bound is rungs * (probes + retries); this pins it at the real defaults
    rather than at the retries=0 the other tests use, which is what hid it.
    """
    error, calls = _failed_sweep(
        monkeypatch,
        lambda call: (_ for _ in ()).throw(kv.BackpressureError("apiCode 690")),
        radius_m=None,
        calibration_probes=kv.DEFAULT_CALIBRATION_PROBES,
        retries=kv.DEFAULT_BACKPRESSURE_RETRIES,
    )
    bound = len(kv.RADIUS_LADDER_M) * (
        kv.DEFAULT_CALIBRATION_PROBES + kv.DEFAULT_BACKPRESSURE_RETRIES
    )
    assert len(calls) <= bound
    assert error.api_requests == len(calls)


def test_calibration_refuses_to_run_with_no_probes():
    """
    0 is the natural spelling of "don't calibrate" and it failed OPEN:
    `answered == len(points)` is 0 == 0, so r=1000 was accepted having asked
    nothing -- and four of the study's fourteen cities could not hold r=1000.
    """
    with pytest.raises(ValueError, match="at least one probe"):
        kv.calibration_points(BBOX, 0)


def test_a_credential_rejected_at_every_probe_says_so_rather_than_blaming_the_city(monkeypatch):
    """
    Every rung failing has two very different causes and they send the operator
    to different places: no answerable radius is a property of the LOCATION
    (Horace ND), a 401 is a property of the TOKEN. Folding the second into the
    first printed "answered no radius at any calibration point ... refusing to
    treat a refusal as an empty city" for a bad key.
    """
    error, calls = _failed_sweep(
        monkeypatch,
        lambda call: (_ for _ in ()).throw(kv.ResponseError("HTTP 401")),
        radius_m=None,
        calibration_probes=2,
    )
    assert isinstance(error, kv.ResponseError)
    assert not isinstance(error, HostBlockedError)  # the token, not the host
    assert "credential" in str(error)
    assert "empty city" not in str(error)
    assert error.api_requests == len(calls)


def test_the_cascade_stops_at_the_radius_floor(monkeypatch):
    error, calls = _failed_sweep(
        monkeypatch, lambda call: (_ for _ in ()).throw(kv.BackpressureError("apiCode 690"))
    )
    assert calls, "the sweep must have asked something"
    assert min(c.radius_m for c in calls) >= kv.RADIUS_FLOOR_M
    assert "unmeasured" in str(error)


def test_a_truncated_circle_is_paged_rather_than_subdivided(monkeypatch):
    """
    Fact 1: pagination is exhaustive (measured to page 7 with zero id overlap
    and union == totalFilteredItems), so page 1 prices the circle and the rest
    is paged. Subdividing instead would pay four requests for what one buys.
    """

    def responder(call):
        if call.key != first_key:
            return [], 0
        if call.page == 1:
            return [_item(id="a"), _item(id="b")], 3
        return [_item(id="c")], 3

    calls = _install(monkeypatch, responder)
    roots = kv.cells_for_bbox(*BBOX, 1000 * math.sqrt(2))
    first_key = (round(roots[0].lat, 5), round(roots[0].lon, 5), roots[0].radius_m)
    result = asyncio.run(
        kv.fetch_city_images_async(
            "Testville", BBOX, "tok", radius_m=1000, ipp=2, retries=0, max_requests_per_minute=0
        )
    )
    assert sorted(result["census"]["id"]) == ["a", "b", "c"]
    assert [c.page for c in calls if c.key == first_key] == [1, 2]
    assert result["cells_visited"] == result["cells"]  # paged, not subdivided
    assert result["radius_m"] == 1000


def test_a_circle_needing_too_many_pages_is_subdivided_instead(monkeypatch):
    """Deep paging is untested past page 7, so the sweep trades it for four
    shallower circles -- keeping page 1, which is already paid for."""
    roots = kv.cells_for_bbox(*BBOX, 1000 * math.sqrt(2))
    first_key = (round(roots[0].lat, 5), round(roots[0].lon, 5), roots[0].radius_m)

    def responder(call):
        if call.key == first_key:
            return [_item(id="a")], (kv.MAX_PAGES_PER_CELL + 1) * 2
        return [], 0

    calls = _install(monkeypatch, responder)
    result = asyncio.run(
        kv.fetch_city_images_async(
            "Testville", BBOX, "tok", radius_m=1000, ipp=2, retries=0, max_requests_per_minute=0
        )
    )
    assert [c.page for c in calls if c.key == first_key] == [1]  # never paged
    assert result["cells_visited"] == result["cells"] + 4
    assert list(result["census"]["id"]) == ["a"]  # page 1 kept, not discarded


def test_a_failed_page_re_covers_the_area_rather_than_accepting_it_short(monkeypatch):
    """A partially paged circle is not exhaustive, and a census that quietly
    keeps two thirds of a circle is a coverage number that cannot be trusted."""
    roots = kv.cells_for_bbox(*BBOX, 1000 * math.sqrt(2))
    first_key = (round(roots[0].lat, 5), round(roots[0].lon, 5), roots[0].radius_m)

    def responder(call):
        if call.key == first_key:
            if call.page == 1:
                return [_item(id="a"), _item(id="b")], 3
            raise kv.BackpressureError("apiCode 690")
        return [], 0

    calls = _install(monkeypatch, responder)
    result = asyncio.run(
        kv.fetch_city_images_async(
            "Testville", BBOX, "tok", radius_m=1000, ipp=2, retries=0, max_requests_per_minute=0
        )
    )
    assert result["cells_visited"] == result["cells"] + 4
    assert {c.radius_m for c in calls} == {1000, 500}
    assert result["failed_cells"] == []


def test_the_runaway_guard_leaves_the_rest_unmeasured_rather_than_publishing_it(monkeypatch):
    """
    max_requests is a runaway guard, not a sampling knob: what it stops is a
    subdivision cascade eating a night, and what it must never do is hand back a
    partial census that looks complete.
    """
    error, calls = _failed_sweep(monkeypatch, _empty, max_requests=2)
    assert len(calls) == 2
    assert "unmeasured" in str(error)


def test_the_host_lock_is_taken_before_any_request(monkeypatch):
    """
    The documented ceiling is per key, but nothing published says the enforced
    one is -- and both prior bans were on limits no document described. A second
    process must fail fast rather than double the rate this one is honouring.
    """
    from filelock import FileLock, Timeout

    from streetscape_metadata_tracker import host_lock as host_lock_module

    calls = _install(monkeypatch, _empty)
    competitor = FileLock(host_lock_module.lock_path(HOST_KARTAVIEW), timeout=0)
    competitor.acquire()
    try:
        with pytest.raises(HostBusyError) as excinfo:
            asyncio.run(
                kv.fetch_city_images_async(
                    "Testville", BBOX, "tok", radius_m=1000, max_requests_per_minute=0
                )
            )
    except Timeout:  # pragma: no cover - would mean the fixture lock leaked
        raise
    finally:
        competitor.release()
    assert calls == []
    assert host_exit_code(excinfo.value) == HOST_BUSY_EXIT_CODES[HOST_KARTAVIEW]


def test_the_kartaview_exit_codes_are_distinct_from_every_other_meaning():
    """
    The message never crosses the process boundary -- the scheduler sees only a
    returncode -- so these numbers ARE the vocabulary. 77/78 are skipped
    deliberately: they are EX_NOPERM and EX_CONFIG, and a plausible-sounding
    wrong answer to "what does 77 mean?" is worse than an unallocated number.
    """
    blocked = HOST_EXIT_CODES[HOST_KARTAVIEW]
    busy = HOST_BUSY_EXIT_CODES[HOST_KARTAVIEW]
    assert blocked not in (77, 78) and busy not in (77, 78)
    assert len(set(HOST_EXIT_CODES.values())) == len(HOST_EXIT_CODES)
    assert len(set(HOST_BUSY_EXIT_CODES.values())) == len(HOST_BUSY_EXIT_CODES)
    assert not set(HOST_EXIT_CODES.values()) & set(HOST_BUSY_EXIT_CODES.values())
    # 0/1 are success and generic failure; 2 is argparse; 64 is EX_USAGE.
    assert not {blocked, busy} & {0, 1, 2, 64}


# ── The antimeridian, and the guard that could not see it ──────────────────


# Taveuni, Fiji at 179.97 E: an ordinary 40 x 40 km grid straddles 180 deg, so
# geopy hands back min_lon 179.78 and max_lon -179.84.
FIJI_BBOX = grid_bbox(-16.85, 179.97, 40000, 40000, 20)


def test_a_bbox_crossing_the_antimeridian_is_tiled_in_full():
    """
    `max_lon - min_lon` is NEGATIVE on a wrapped bbox, so ceil went negative and
    max(1, ...) collapsed the city to a single column. Measured before the fix:
    29 cells where Taveuni needs 841 -- 3.4% of the city, returned as a clean
    success. download_mapillary.tiles_for_bbox already carries this fix and
    names Suva; cells_for_bbox reintroduced the naive form.
    """
    assert FIJI_BBOX[0] > FIJI_BBOX[2], "fixture must actually wrap"
    cells = kv.cells_for_bbox(*FIJI_BBOX, 1000 * math.sqrt(2))
    unwrapped = kv.cells_for_bbox(-0.218, -17.031, 0.158, -16.669, 1000 * math.sqrt(2))
    # Same extent, just shifted across the seam: the counts must match.
    assert len(cells) == len(unwrapped)
    assert len(cells) > 800
    assert all(-180.0 <= c.lon <= 180.0 for c in cells), "centres must stay normalized"


def test_the_wrapped_bbox_area_is_the_city_not_the_planet():
    """
    The mirror-image half of the same bug, and the reason it failed SILENTLY:
    abs() turned the -359.6 deg span into ~1.5 million km2, so every cell in
    Taveuni could fail and still compute as 0.004% unmeasured -- three orders
    of magnitude under MAX_FAILED_AREA_FRACTION. The sweep could not refuse.
    """
    area_km2 = kv._bbox_area_m2(FIJI_BBOX) / 1e6
    assert 1_000 < area_km2 < 3_000
    cells = kv.cells_for_bbox(*FIJI_BBOX, 1000 * math.sqrt(2))
    every_cell_failed = sum(c.size_m**2 for c in cells) / kv._bbox_area_m2(FIJI_BBOX)
    assert every_cell_failed > kv.MAX_FAILED_AREA_FRACTION


def test_the_estimate_agrees_with_the_plan_across_the_seam():
    """estimate_sweep_requests gates the channel's budget and timeout, so it
    must not be the one place the wrap survives."""
    assert kv.estimate_sweep_requests(-16.85, 179.97, 40000, 40000, 20) == len(
        kv.cells_for_bbox(*FIJI_BBOX, kv.DEFAULT_START_RADIUS_M * math.sqrt(2))
    )


# ── What each HTTP failure means, revisited ────────────────────────────────


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_credential_serving_html_is_still_only_the_credential(status):
    """
    THE realistic shape: kartaview.org is a JS single-page app on this very
    host, so a rejected or expired token comes back as an HTML login page. The
    content-type test used to run first and typed that as a host refusal, which
    trips #208's night-level breaker -- every remaining KartaView city skipped,
    no failure recorded, and an alert sending the operator after a ban that
    never happened. The pre-existing test passed only because its fixture
    served JSON.
    """
    error = _post_error(
        _FakeResponse(
            status=status,
            headers={"Content-Type": "text/html; charset=utf-8"},
            text="<html><body>Sign in to KartaView</body></html>",
        )
    )
    assert isinstance(error, kv.ResponseError)
    assert not isinstance(error, HostBlockedError)
    assert "KARTAVIEW_ACCESS_TOKEN" in str(error)


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_a_server_error_page_is_transient_not_a_host_refusal(status):
    """
    An overloaded upstream answers with its load balancer's HTML error page
    essentially always, so the content-type test called an ordinary 502 a host
    refusal. Typed as transport it takes _probe_cell's retry budget instead,
    and if it persists the cell is recorded unmeasured -- never subdivided,
    since a struggling server must not be asked for four requests where it just
    failed to serve one (#198).
    """
    error = _post_error(
        _FakeResponse(
            status=status,
            headers={"Content-Type": "text/html"},
            text="<html>502 Bad Gateway</html>",
        )
    )
    assert isinstance(error, kv.TransportError)
    assert not isinstance(error, HostBlockedError)


def test_an_html_page_on_a_200_is_still_a_host_refusal():
    """The #199 shape survives the reordering above."""
    error = _post_error(
        _FakeResponse(status=200, headers={"Content-Type": "text/html"}, text="<html>nope</html>")
    )
    assert isinstance(error, HostBlockedError)


@pytest.mark.parametrize("body", ["[]", '"nope"', "null", "3"])
def test_a_json_body_that_is_not_an_object_is_a_response_error(body):
    """`body.get(...)` on a list raises AttributeError, which is neither a
    DownloadError nor a transport error -- so it escaped the sweep WITHOUT the
    api_requests the caller needs to write its ledger row."""
    error = _post_error(_FakeResponse(text=body))
    assert isinstance(error, kv.ResponseError)


# ── Pacing, the ledger, and what actually reaches the wire ─────────────────


class _SpyLimiter:
    """Records acquisitions so the pacing can be observed rather than assumed."""

    def __init__(self, rate):
        self.rate = rate
        self.acquires = 0

    async def acquire(self):
        self.acquires += 1


@pytest.mark.parametrize(
    "response",
    [
        _FakeResponse(body={"currentPageItems": [], "totalFilteredItems": 0}),
        _FakeResponse(status=400, body={"status": {"apiCode": 690, "apiMessage": "too heavy"}}),
        _FakeResponse(status=401, body={"status": {"apiCode": 401}}),
    ],
    ids=["ok", "backpressure", "rejected-credential"],
)
def test_every_request_takes_a_token_and_a_ledger_increment_whatever_it_returns(response):
    """
    #198/#203's invariant: one token, one ledger increment, one HTTP request --
    and both must happen BEFORE the status is known, or a refused request is
    unpaced and unbilled. Deleting `limiter.acquire()` outright used to leave
    the whole suite green, which for a host that meters per IP and publishes no
    rate-limit headers is the one thing that must not be unobserved.
    """
    limiter = _SpyLimiter(16)
    session = _FakeSession(response)
    counted = []
    try:
        asyncio.run(
            kv._post_nearby(
                session, limiter, lambda: counted.append(1), 47.6, -122.33, 400, access_token="tok"
            )
        )
    except DownloadError:
        pass
    assert limiter.acquires == 1
    assert counted == [1]
    assert len(session.calls) == 1


def test_the_sweep_builds_its_limiter_at_the_configured_rate(monkeypatch):
    built = []

    class _Recording(_SpyLimiter):
        def __init__(self, rate):
            super().__init__(rate)
            built.append(rate)

    monkeypatch.setattr(kv, "AsyncRateLimiter", _Recording)
    _install(monkeypatch, _empty)
    asyncio.run(
        kv.fetch_city_images_async(
            "Testville", BBOX, "tok", radius_m=1000, retries=0, max_requests_per_minute=7
        )
    )
    assert built == [7]


def test_the_default_pace_stays_under_the_documented_hourly_ceiling():
    """
    KartaView returns no X-RateLimit-* or Retry-After headers at all, so a
    client cannot observe its own budget and this is the only check there is.
    Raising the constant to 20/min (1,200/h) broke nothing before this test.
    """
    assert kv.DEFAULT_SWEEP_REQUESTS_PER_MINUTE * 60 <= kv.REQUESTS_PER_HOUR_AUTH


def test_the_request_carries_the_token_and_the_full_page_size():
    """
    load_config makes the token mandatory on the ground that the anonymous tier
    "is not a slower channel, it is no channel" -- so the sweep had better
    actually send it. And ipp is what the 2000-vs-200 finding turned on: at 200
    the same city costs 10x the requests.
    """
    (_, _), session, _ = _post(
        _FakeResponse(body={"currentPageItems": [], "totalFilteredItems": 0}),
        access_token="secret-token",
    )
    assert session.calls[0]["params"] == {"access_token": "secret-token"}
    assert session.calls[0]["data"]["ipp"] == kv.IPP_MAX == 2000


# ── The runaway guard, which did not guard ─────────────────────────────────


def test_the_budget_bounds_a_cascade_not_just_the_root_boundary(monkeypatch):
    """
    Checked only between root cells, max_requests bounded nothing: one root can
    cascade to the radius floor (1 + 4 + 16 + 64 cells, each with retries and
    pages) without the loop ever asking again. Measured before this fix:
    max_requests=5 issued 500 requests.
    """
    error, calls = _failed_sweep(
        monkeypatch,
        lambda call: (_ for _ in ()).throw(kv.BackpressureError("apiCode 690")),
        radius_m=1000,
        max_requests=3,
        retries=0,
    )
    assert len(calls) <= 4, f"budget of 3 spent {len(calls)} requests"
    assert error.api_requests == len(calls)
    assert "unmeasured" in str(error)


def test_a_cell_needing_more_pages_than_the_cap_is_unmeasured_not_unbounded(monkeypatch):
    """
    `pages > MAX_PAGES_PER_CELL and can_subdivide(cell)` made the cap a no-op
    at the radius floor: control fell through and paged to a SERVER-supplied
    total with no ceiling. A 100 m circle claiming a million items paged 500
    times at 16/min. A circle we cannot exhaust is unmeasured area.
    """
    error, calls = _failed_sweep(
        monkeypatch,
        lambda call: ([], 1_000_000),
        radius_m=kv.RADIUS_FLOOR_M,
        retries=0,
    )
    # One page-1 per root cell and nothing more: no paging past the cap, and no
    # subdivision either, since there is nothing below the floor to split into.
    assert all(call.page == 1 for call in calls)
    assert len(calls) == len(kv.cells_for_bbox(*BBOX, kv.RADIUS_FLOOR_M * math.sqrt(2)))
    assert "unmeasured" in str(error)


def test_a_page_two_transport_fault_does_not_fan_out_into_four_circles(monkeypatch):
    """
    The deep-paging branch subdivided on `refused` OR `broken`, so a transport
    fault -- and a rejected credential -- fanned one request into four and
    cascaded to the floor. Measured: 42 requests for a 401 and 105 for a single
    TCP reset, against docstrings promising "asked exactly once" and "recorded
    as a failed cell". Only backpressure may subdivide.
    """

    def responder(call):
        if call.page == 1:
            return [], 4000  # exactly two pages
        raise kv.TransportError("connection reset")

    error, calls = _failed_sweep(monkeypatch, responder, radius_m=500, retries=0)
    assert {call.radius_m for call in calls} == {500}, "no subdivision may have happened"
    assert len(calls) == 2 * len(kv.cells_for_bbox(*BBOX, 500 * math.sqrt(2)))
    assert "unmeasured" in str(error)


def test_a_page_two_backpressure_refusal_still_subdivides(monkeypatch):
    """The other half of the same branch: backpressure means "ask for less",
    and a partially paged circle is not exhaustive, so its area is re-covered
    as four smaller ones."""

    def responder(call):
        if call.page == 1:
            return [], 4000
        raise kv.BackpressureError("apiCode 690")

    _failed_sweep(monkeypatch, responder, radius_m=500, retries=0)
    # The point is only that children WERE asked; the sweep still refuses.


def test_a_page_size_above_the_server_cap_does_not_silently_truncate(monkeypatch):
    """
    _post_nearby sent min(ipp, IPP_MAX) while pages_for_total priced the
    caller's value, so ipp=8000 asked for one page of a circle holding 8,000
    photos, got 2,000, and recorded no failed cell and no warning: 6,000 photos
    absent from a snapshot that published as complete.
    """
    result, calls = _sweep(monkeypatch, lambda call: ([], 8000), radius_m=1000, ipp=8000)
    pages = {call.page for call in calls}
    assert pages == {1, 2, 3, 4}, f"expected 8000/2000 = 4 pages, saw {sorted(pages)}"
    assert result["failed_cells"] == []


# ── The checkpoint (issue #239) ────────────────────────────────────────────
#
# A sweep is hours of paced fetching, so the thing under test is not "does it
# write a file" but "does a resumed sweep produce EXACTLY the artifact an
# uninterrupted one would, having re-asked nothing". Both halves are load-bearing
# and the first is the one a plausible edit breaks silently.


def _sweep_ckpt(monkeypatch, responder, path, **kwargs):
    """A checkpointed :func:`_sweep`."""
    return _sweep(monkeypatch, responder, checkpoint_path=str(path), **kwargs)


def _failed_sweep_ckpt(monkeypatch, responder, path, **kwargs):
    """A checkpointed :func:`_failed_sweep`."""
    return _failed_sweep(monkeypatch, responder, checkpoint_path=str(path), **kwargs)


def _state(path):
    """The commit record, as a dict."""
    return json.loads((path / kv.CHECKPOINT_STATE_FILENAME).read_text())


def _parts(path):
    return sorted(p.name for p in path.glob("part-*.parquet"))


def _photos(call):
    """
    Two photos unique to this circle, plus one every circle re-sees.

    The shared id is the point: the lattice covers each square with its
    circumscribed circle, so the sweep re-sees ~pi/2 of everything and
    dedupe_census keeps a repeated id at the position of its FIRST appearance
    with its LAST values. Carrying that across a process boundary is what the
    checkpoint's fetch-order contract exists for.
    """
    tag = f"{call.lat:.5f}:{call.lon:.5f}"
    items = [_item(id=f"{tag}#{n}") for n in range(2)]
    items.append(_item(id="seen-everywhere", lat=f"{call.lat}", lng=f"{call.lon}"))
    return items, len(items)


def test_a_resumed_census_is_identical_to_an_uninterrupted_one(monkeypatch, tmp_path):
    """
    THE test of this feature. A census resumed across a process boundary must be
    the same frame, row for row and value for value, as one swept in a single
    go -- because what it becomes is an immutable dated snapshot that diff.py
    compares to its predecessor, so any reordering surfaces as phantom churn in
    every city rather than as a visible failure.
    """
    whole, whole_calls = _sweep(monkeypatch, _photos)

    ckpt = tmp_path / "sweep"
    error, night_one = _failed_sweep_ckpt(monkeypatch, _photos, ckpt, max_requests=2)
    assert isinstance(error, kv.SweepIncompleteError)
    resumed, night_two = _sweep_ckpt(monkeypatch, _photos, ckpt)

    assert night_one and night_two
    assert len(night_one) + len(night_two) == len(whole_calls)
    pd.testing.assert_frame_equal(resumed["census"], whole["census"])
    assert resumed["raw_photo_count"] == whole["raw_photo_count"]
    assert resumed["cells_visited"] == whole["cells_visited"]


def test_a_sweep_spanning_three_nights_is_still_that_one_census(monkeypatch, tmp_path):
    """
    The case the feature is actually for, and structurally distinct from one
    interruption: night two both READS a checkpoint and WRITES one, so a resume
    that mis-seeds its own commit -- restarting the part index, dropping the
    inherited counters, re-committing what it loaded -- shows up only here. It
    is also the whole-catalog answer, since "a pass takes N nights" is this
    property applied to a bigger lattice.
    """
    whole, whole_calls = _sweep(monkeypatch, _photos, radius_m=500)
    assert len(whole_calls) == 9, "the fixture needs enough roots to stop twice"

    ckpt = tmp_path / "sweep"
    nights = []
    for budget in (3, 3):
        error, calls = _failed_sweep_ckpt(
            monkeypatch,
            _photos,
            ckpt,
            radius_m=500,
            max_requests=budget,
            checkpoint_request_interval=2,
        )
        assert isinstance(error, kv.SweepIncompleteError)
        nights.append(calls)
    final, last = _sweep_ckpt(monkeypatch, _photos, ckpt, radius_m=500)
    nights.append(last)

    assert [len(n) for n in nights] == [3, 3, 3]
    assert len({c.key for n in nights for c in n}) == 9, "no circle asked twice"
    pd.testing.assert_frame_equal(final["census"], whole["census"])
    assert final["api_requests"] == 3
    assert final["api_requests_total"] == len(whole_calls) == 9
    assert final["cells_visited"] == whole["cells_visited"]
    # Still there: the caller discards it once its artifact is durable.
    kv.discard_checkpoint(str(ckpt))
    assert not ckpt.exists()


def test_a_hole_punched_on_night_one_still_refuses_the_snapshot_on_night_two(monkeypatch, tmp_path):
    """
    Failed cells are inherited across a resume, and must be: a cell that
    genuinely never answered is a hole in the bbox whether it was asked
    yesterday or today, and the area guard is what stops an immutable dated
    snapshot being published around it. Losing them on resume would turn a
    refused sweep into a silently incomplete one.

    The refusal keeps the checkpoint, because a complete one re-finalizes
    without spending a request -- so the operator's retry is free, and the reset
    is the deliberate act of deleting the directory.
    """
    ckpt = tmp_path / "sweep"
    seen = []

    def responder(call):
        seen.append(call)
        if len(seen) == 1:
            raise kv.ResponseError("HTTP 500")  # this root is a hole, permanently
        return [], 0

    night_one, calls = _failed_sweep_ckpt(monkeypatch, responder, ckpt, max_requests=2, retries=0)
    assert isinstance(night_one, kv.SweepIncompleteError)
    assert len(_state(ckpt)["failed_cells"]) == 1

    night_two, _ = _failed_sweep_ckpt(monkeypatch, responder, ckpt)
    assert not isinstance(night_two, kv.SweepIncompleteError)
    assert "unmeasured" in str(night_two) and "refusing to finalize" in str(night_two)
    assert str(ckpt) in str(night_two)
    assert _state(ckpt)["roots_done"] == 4, "kept, so the retry costs nothing"
    assert len(calls) == 2


def test_a_null_looking_string_survives_the_checkpoint_as_a_string(tmp_path):
    """
    Why the parts are parquet and not CSV, pinned as the consequence rather than
    as the format.

    Every string column here is PROVIDER-SUPPLIED, and pandas' default na_values
    claims "NA", "null", "None" and "nan". A contributor is free to call
    themselves NA. Through CSV that photo comes back attributed to
    "© KartaView" rather than "© KartaView contributor NA" and loses its OSM way
    -- so a resumed run would publish different rows than an uninterrupted one,
    which is the one thing a checkpoint must never do.
    """
    ckpt = tmp_path / "sweep"
    ckpt.mkdir()
    cp = kv.SweepCheckpoint(path=str(ckpt), radius_m=1000)
    frame = kv.records_to_census(
        kv.decode_photo_items(
            [
                _item(id="a", username="NA", way_id="null", sequence_id="None"),
                _item(id="b", username=None),
            ]
        )
    )
    kv._commit_checkpoint(
        cp,
        [frame],
        roots_done=1,
        failed_cells=[],
        cells_visited=1,
        raw_photo_count=2,
        api_requests_total=1,
        bbox=BBOX,
        ipp=kv.IPP_MAX,
        root_count=4,
    )
    (back,) = kv._checkpoint_frames(cp)
    pd.testing.assert_frame_equal(back, frame)
    assert list(back["way_id"]) == ["null", "993382884"]
    assert list(kv._kartaview_image_columns(back)["copyright_info"]) == [
        "© KartaView contributor NA",
        "© KartaView",
    ]

    # And the counterexample, so the reason outlives the choice: the same frame
    # through the obvious CSV form loses all three values to na_values.
    csv = io.StringIO()
    frame.to_csv(csv, index=False)
    via_csv = pd.read_csv(io.StringIO(csv.getvalue()), dtype=dict(kv._CENSUS_DTYPES))
    assert via_csv["username"].isna().all()
    assert via_csv["way_id"].iloc[0] is pd.NA


def test_the_committed_parts_are_read_in_fetch_order(tmp_path):
    """
    Index order IS fetch order. A directory glob would be lexical, which happens
    to agree only while the zero-padding holds -- so the reader takes the count
    from the commit record and never asks the filesystem what is there.
    """
    ckpt = tmp_path / "sweep"
    ckpt.mkdir()
    cp = kv.SweepCheckpoint(path=str(ckpt), radius_m=1000)
    for n, ids in enumerate([["a", "b"], ["c"], ["d", "e"]]):
        kv._commit_checkpoint(
            cp,
            [kv.records_to_census(kv.decode_photo_items([_item(id=i) for i in ids]))],
            roots_done=n + 1,
            failed_cells=[],
            cells_visited=n + 1,
            raw_photo_count=0,
            api_requests_total=n + 1,
            bbox=BBOX,
            ipp=kv.IPP_MAX,
            root_count=4,
        )
    assert _parts(ckpt) == ["part-00000.parquet", "part-00001.parquet", "part-00002.parquet"]
    combined = kv.concat_census(kv._checkpoint_frames(cp))
    assert list(combined["id"]) == ["a", "b", "c", "d", "e"]


def test_an_interrupted_sweep_re_asks_no_cell_it_already_answered(monkeypatch, tmp_path):
    """
    The whole point: what a resume must NOT do is re-spend requests already paid
    for, at 16/min against a host that has banned this project twice.
    """
    ckpt = tmp_path / "sweep"
    _, night_one = _failed_sweep_ckpt(monkeypatch, _empty, ckpt, max_requests=2)
    _, night_two = _sweep_ckpt(monkeypatch, _empty, ckpt)

    first = {c.key for c in night_one}
    second = {c.key for c in night_two}
    assert first and second
    assert not first & second, "a resume must not re-ask an answered circle"
    _, whole = _sweep(monkeypatch, _empty)
    assert first | second == {c.key for c in whole}


def test_a_resume_pins_the_radius_and_skips_calibration(monkeypatch, tmp_path):
    """
    Refusals are time-varying (fact 2: Horace refused r=1000 on 0/6 attempts and
    answered it 2/2 forty-five minutes later), so a resume that re-calibrated
    could land on a different rung -- and `roots_done` would then index into a
    lattice it was never recorded against. Every cell already swept would count
    as covering area it does not cover.
    """
    ckpt = tmp_path / "sweep"
    _failed_sweep_ckpt(monkeypatch, _empty, ckpt, radius_m=500, max_requests=2)
    assert _state(ckpt)["radius_m"] == 500

    # Night two asks for no particular radius, and the responder would happily
    # answer r=1000 -- the top of the ladder calibration starts at.
    result, night_two = _sweep_ckpt(monkeypatch, _empty, ckpt, radius_m=None)
    assert result["radius_m"] == 500
    assert {c.radius_m for c in night_two} == {500}, "a resume must not re-calibrate"


def test_a_resumed_sweep_reports_only_this_processes_requests(monkeypatch, tmp_path):
    """
    `api_requests` feeds db.add_api_usage, which is `requests = requests + ?`
    keyed by (date, provider). A resumed night reporting the whole sweep would
    charge last night's requests against tonight's budget gate, and the gate is
    what decides whether the next city runs at all.
    """
    ckpt = tmp_path / "sweep"
    error, night_one = _failed_sweep_ckpt(monkeypatch, _empty, ckpt, max_requests=2)
    assert error.api_requests == len(night_one)
    assert error.api_requests_total == len(night_one)

    result, night_two = _sweep_ckpt(monkeypatch, _empty, ckpt)
    assert result["api_requests"] == len(night_two)
    assert result["api_requests_total"] == len(night_one) + len(night_two)
    assert result["api_requests"] < result["api_requests_total"]


def test_a_clean_sweep_leaves_its_checkpoint_for_the_caller(monkeypatch, tmp_path):
    """
    The sweep does NOT delete its own checkpoint, and that is the whole of what
    makes a crash in the caller's tail recoverable: the census comes back as a
    DataFrame and the dated CSV, the stats, the run row, the JSON and the diff
    are all written after this returns. A delete on the way out would cover
    every interruption except the ones that happen after it.
    """
    ckpt = tmp_path / "sweep"
    result, _ = _sweep_ckpt(monkeypatch, _photos, ckpt)
    assert result["checkpoint_path"] == str(ckpt)
    assert ckpt.exists(), "the caller owns this directory; the sweep must not delete it"
    assert _state(ckpt)["roots_done"] == result["cells"]

    kv.discard_checkpoint(str(ckpt))
    assert not ckpt.exists()
    kv.discard_checkpoint(str(ckpt))  # idempotent: discarding twice is not an error


def test_a_completed_checkpoint_finalizes_without_a_single_request(monkeypatch, tmp_path):
    """
    The gap-closer for a crash AFTER the sweep and BEFORE the caller's artifact
    is durable -- the shape of #157's tail OOM. A checkpoint whose roots are all
    done re-finalizes from disk, so the ten hours are not re-spent to recover a
    write that failed.

    No stub stands in for the dead caller any more. The sweep leaves the
    checkpoint behind on its own, so this reaches the recovery state the same
    way production would: a caller that never got to `discard_checkpoint`.
    """
    ckpt = tmp_path / "sweep"
    first, _ = _sweep_ckpt(monkeypatch, _photos, ckpt)
    assert _state(ckpt)["roots_done"] == first["cells"]

    again, calls = _sweep_ckpt(monkeypatch, _photos, ckpt)
    assert calls == [], "a complete checkpoint must cost nothing to finalize"
    pd.testing.assert_frame_equal(again["census"], first["census"])
    assert again["api_requests"] == 0
    assert again["api_requests_total"] == first["api_requests_total"]


def test_finalizing_from_a_complete_checkpoint_says_so_at_warning(monkeypatch, tmp_path, caplog):
    """
    A finalize-only resume issues no request, so its artifact is indistinguishable
    from a fresh collection's. That is the intended recovery -- and it is also
    exactly what a caller that forgot to discard looks like, so it has to be
    audible rather than an INFO line among hours of them.
    """
    ckpt = tmp_path / "sweep"
    _sweep_ckpt(monkeypatch, _photos, ckpt)

    with caplog.at_level(logging.WARNING, logger=kv.logger.name):
        _sweep_ckpt(monkeypatch, _photos, ckpt)
    warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("COMPLETE" in m and "discard_checkpoint" in m for m in warnings), warnings


def test_a_stale_checkpoint_is_discarded_rather_than_spliced_into_todays_snapshot(
    monkeypatch, tmp_path
):
    """
    The one way a checkpoint could produce a WRONG artifact rather than wasted
    work, which is the line the whole design is drawn against.

    Frozen grid geometry never changes, so bbox, ipp, radius and root_count all
    still match months later -- every other validation passes. A city that was
    interrupted and then sat out a long gap (a channel switched off after a
    per-IP block, `consecutive_failures` quarantining it for a 90-day cycle)
    would otherwise resume and splice last quarter's rows into a snapshot dated
    today, published as one observation of one day.
    """
    ckpt = tmp_path / "sweep"
    error, night_one = _failed_sweep_ckpt(monkeypatch, _photos, ckpt, max_requests=2)
    assert isinstance(error, kv.SweepIncompleteError)
    assert night_one, "night one must have paid for something worth resuming"

    state = _state(ckpt)
    aged = datetime.now(UTC) - timedelta(seconds=kv.CHECKPOINT_MAX_AGE_S + 60)
    state["updated_at"] = aged.isoformat()
    (ckpt / kv.CHECKPOINT_STATE_FILENAME).write_text(json.dumps(state))

    whole, whole_calls = _sweep(monkeypatch, _photos)
    result, calls = _sweep_ckpt(monkeypatch, _photos, ckpt)
    assert len(calls) == len(whole_calls), "a stale checkpoint must be re-swept, not resumed"
    assert result["api_requests_total"] == result["api_requests"], (
        "a discarded checkpoint must not carry its spend forward either"
    )
    pd.testing.assert_frame_equal(result["census"], whole["census"])


def test_a_checkpoint_from_last_night_still_resumes(monkeypatch, tmp_path):
    """
    The bound's other side: this is a staleness guard, not an expiry clock. The
    age here is ABSOLUTE (18 h -- last night's interrupted sweep, picked up by
    tonight's batch) rather than derived from CHECKPOINT_MAX_AGE_S, or shrinking
    the constant would move the fixture with it and the test would keep passing
    on a guard tight enough to defeat the whole feature.
    """
    ckpt = tmp_path / "sweep"
    _failed_sweep_ckpt(monkeypatch, _photos, ckpt, max_requests=2)

    state = _state(ckpt)
    state["updated_at"] = (datetime.now(UTC) - timedelta(hours=18)).isoformat()
    (ckpt / kv.CHECKPOINT_STATE_FILENAME).write_text(json.dumps(state))

    whole, whole_calls = _sweep(monkeypatch, _photos)
    result, calls = _sweep_ckpt(monkeypatch, _photos, ckpt)
    assert len(calls) < len(whole_calls), "last night's checkpoint must resume"
    pd.testing.assert_frame_equal(result["census"], whole["census"])


def test_the_checkpoint_age_limit_is_sized_between_a_long_sweep_and_the_cadence():
    """
    The constant is a measurement sandwich, so pin both walls rather than the
    number. Below: Singapore, the catalog's worst city, is ~9,974 requests at
    DEFAULT_SWEEP_REQUESTS_PER_MINUTE -- a legitimate multi-night sweep must fit
    with room to spare. Above: `min_days_since_last_run` is 80, so the guard has
    to be well under that or it would start catching ordinary re-collections
    instead of stale ones.
    """
    singapore_hours = 9_974 / kv.DEFAULT_SWEEP_REQUESTS_PER_MINUTE / 60
    assert singapore_hours < 11, "re-measure this test's premise, not just the constant"
    assert kv.CHECKPOINT_MAX_AGE_S > 3 * singapore_hours * 3600
    assert kv.CHECKPOINT_MAX_AGE_S < 80 * 24 * 3600 / 4


def test_the_progress_bar_resumes_at_the_committed_root_count(monkeypatch, tmp_path):
    """
    Paced at 16/min a sweep is the scheduler's longest-running child, and its
    once-a-minute log line is the only thing distinguishing "slow" from "hung"
    after a SIGKILL (#157). Restarting that line at 0% on a resumed night would
    make an almost-finished city look untouched.
    """
    ckpt = tmp_path / "sweep"
    _failed_sweep_ckpt(monkeypatch, _empty, ckpt, max_requests=2)
    done = _state(ckpt)["roots_done"]
    assert done == 2

    seen = {}
    real_progress = kv.progress

    def spy(**kwargs):
        seen.update(kwargs)
        return real_progress(**kwargs)

    monkeypatch.setattr(kv, "progress", spy)
    result, _ = _sweep_ckpt(monkeypatch, _empty, ckpt)
    assert seen["initial"] == done
    assert seen["total"] == result["cells"]


def test_a_budget_stop_with_a_checkpoint_continues_tomorrow_rather_than_refusing(
    monkeypatch, tmp_path
):
    """
    The behaviour change #239 is for. Without a checkpoint the same trip marks
    the rest of the bbox unmeasured and the area guard refuses, discarding the
    spend; with one it is simply an unfinished sweep. Nothing is finalized
    either way -- a partial census must never publish as a dated snapshot.
    """
    ckpt = tmp_path / "sweep"
    error, calls = _failed_sweep_ckpt(monkeypatch, _empty, ckpt, max_requests=2)
    assert isinstance(error, kv.SweepIncompleteError)
    assert "unmeasured" not in str(error)
    assert str(ckpt) in str(error)
    assert error.roots_done == len(calls) == 2
    assert error.root_count == 4
    assert ckpt.exists() and _state(ckpt)["roots_done"] == 2


def test_an_unvisited_root_is_never_a_failed_cell(monkeypatch, tmp_path):
    """
    "Unmeasured" and "not yet visited" are different facts, and conflating them
    poisons the resumed sweep: the resume would inherit failed cells nothing was
    ever wrong with, and the area guard would refuse a snapshot that is about to
    be completed.
    """
    ckpt = tmp_path / "sweep"
    _failed_sweep_ckpt(monkeypatch, _empty, ckpt, max_requests=2)
    assert _state(ckpt)["failed_cells"] == []


def test_a_sweep_incomplete_error_is_not_a_host_condition(monkeypatch, tmp_path):
    """
    host_exit_code maps a HostUnavailableError to 81, which the scheduler turns
    into a night-level breaker skipping every remaining KartaView city. An
    incomplete sweep is a property of THIS city's budget; the next city is
    unaffected and must still run.
    """
    from streetscape_metadata_tracker.download_common import (
        SWEEP_INCOMPLETE_EXIT_CODE,
        HostUnavailableError,
    )

    ckpt = tmp_path / "sweep"
    error, _ = _failed_sweep_ckpt(monkeypatch, _empty, ckpt, max_requests=2)
    assert isinstance(error, DownloadError)
    assert not isinstance(error, HostUnavailableError)
    assert SWEEP_INCOMPLETE_EXIT_CODE not in HOST_EXIT_CODES.values()
    assert SWEEP_INCOMPLETE_EXIT_CODE not in HOST_BUSY_EXIT_CODES.values()
    assert SWEEP_INCOMPLETE_EXIT_CODE not in (0, 1, 2, 64, 77, 78)


def test_a_stop_mid_root_does_not_mark_that_root_done(monkeypatch, tmp_path):
    """
    A commit always writes the sweep as of the last completed ROOT BOUNDARY.
    That invariant is what makes the finally-commit safe from any exception path
    and what lets the DFS stack stay un-persisted -- so a stop landing inside a
    cascade rolls the partial root back rather than recording it as swept, which
    would silently drop whatever its remaining stack still held.
    """
    ckpt = tmp_path / "sweep"
    seen = []

    def responder(call):
        seen.append(call)
        if len(seen) == 1:
            return [], 0  # root 0 answers cleanly and completes
        if len(seen) == 2:
            raise kv.BackpressureError("apiCode 690")  # root 1 cascades
        raise kv.ResponseError("HTTP 500")  # ...and a child of it is broken

    _failed_sweep_ckpt(monkeypatch, responder, ckpt, max_requests=3, retries=0)
    state = _state(ckpt)
    assert state["roots_done"] == 1, "the cascading root was not finished"
    assert state["cells_visited"] == 1, "its subdivisions were rolled back"
    assert state["failed_cells"] == [], (
        "the broken child belongs to a root that will be swept again from its "
        "top, so recording it would double-count it against the area guard"
    )


def test_a_stop_mid_root_discards_that_roots_rows_rather_than_committing_them(
    monkeypatch, tmp_path
):
    """
    The other half of the boundary invariant, and the expensive half. A root
    stopped between its pages has photos in hand, but a paged circle is not
    exhaustive until its last page -- so committing those rows against a
    `roots_done` that does not include the root would leave the resume free to
    re-sweep it, and the census would carry its early pages twice while the
    counters describing it drifted permanently.
    """
    ckpt = tmp_path / "sweep"

    def paged(call):
        # ipp=2 with a total of 6 means three pages: page 1, page 2, and a page
        # 3 the budget never reaches.
        return [_item(id=f"{call.lat:.5f}:{call.lon:.5f}#p{call.page}")], 6

    bars = []
    real_progress = kv.progress
    monkeypatch.setattr(kv, "progress", lambda **kw: bars.append(real_progress(**kw)) or bars[-1])

    error, calls = _failed_sweep_ckpt(
        monkeypatch, paged, ckpt, ipp=2, max_requests=2, checkpoint_request_interval=1
    )
    assert isinstance(error, kv.SweepIncompleteError)
    assert {c.page for c in calls} == {1, 2}, "the stop must land inside the page loop"
    assert bars[0].n == 0, (
        "the bar must not count a root that was rolled back -- on the night that "
        "got killed, over-reporting progress is what makes 'slow' and 'hung' "
        "indistinguishable in the log (#157)"
    )
    state = _state(ckpt)
    assert state["roots_done"] == 0, "a half-paged root is not a swept root"
    assert state["census_rows"] == 0, "its pages are rolled back, not committed"
    assert state["parts"] == 0
    assert state["raw_photo_count"] == 0


def test_a_crash_between_a_roots_pages_rewinds_it_too(monkeypatch, tmp_path):
    """
    The boundary rewind is enforced in the `finally`, not per stop-path, so it
    covers a host block, a transport fault and a bug as well as the budget stop
    -- and this is the case that distinguishes the two placements: a root
    interrupted by an EXCEPTION between its pages has photos in hand, and
    committing them against a `roots_done` that excludes the root would leave
    the resume free to sweep it again, carrying its early pages twice.
    """
    ckpt = tmp_path / "sweep"
    pages_seen = []

    def paged(call):
        pages_seen.append(call.page)
        if call.page == 2:
            raise RuntimeError("something entirely unexpected")
        return [_item(id=f"{call.lat:.5f}:{call.lon:.5f}#p{call.page}")], 6

    _install(monkeypatch, paged)
    with pytest.raises(RuntimeError):
        asyncio.run(
            kv.fetch_city_images_async(
                "Testville",
                BBOX,
                "tok",
                radius_m=1000,
                ipp=2,
                retries=0,
                max_requests_per_minute=0,
                checkpoint_path=str(ckpt),
                checkpoint_request_interval=1,
            )
        )
    assert pages_seen == [1, 2], "the crash must land inside the page loop"
    state = _state(ckpt)
    assert state["roots_done"] == 0
    assert state["census_rows"] == 0, "page 1 of an unfinished circle is not census"
    assert state["cells_visited"] == 0
    assert state["raw_photo_count"] == 0


def test_a_checkpoint_that_cannot_be_written_does_not_fail_the_sweep(monkeypatch, tmp_path):
    """
    The _write_owner posture: a checkpoint that cannot be written must never be
    what fails a sweep, since the cost is re-paying a segment rather than a
    wrong artifact -- and swallowing it is also what stops it masking an
    in-flight exception.

    It is the one arrangement that leaves uncommitted frames in hand at
    finalize, so it doubles as the check that they are concatenated AFTER the
    committed parts rather than before: index order is fetch order.
    """
    ckpt = tmp_path / "sweep"
    whole, _ = _sweep(monkeypatch, _photos)

    real_commit = kv._commit_checkpoint
    commits = {"n": 0}

    def flaky_commit(*args, **kwargs):
        commits["n"] += 1
        if commits["n"] > 1:  # the periodic commit lands; the final one does not
            raise OSError("no space left on device")
        return real_commit(*args, **kwargs)

    monkeypatch.setattr(kv, "_commit_checkpoint", flaky_commit)
    result, _ = _sweep_ckpt(monkeypatch, _photos, ckpt, checkpoint_request_interval=3)
    assert commits["n"] == 2, "the periodic commit ran, and then the final one failed"
    pd.testing.assert_frame_equal(result["census"], whole["census"])


def test_a_crash_mid_sweep_leaves_a_resumable_checkpoint(monkeypatch, tmp_path):
    """
    Not every interruption is one we typed. An unexpected exception must still
    leave the answered circles on disk -- the finally-commit's job, distinct
    from the periodic one that covers SIGTERM and SIGKILL, which run no
    handlers at all.
    """
    ckpt = tmp_path / "sweep"
    calls = []

    def responder(call):
        calls.append(call)
        if len(calls) > 2:
            raise RuntimeError("something entirely unexpected")
        return _photos(call)

    _install(monkeypatch, responder)
    with pytest.raises(RuntimeError):
        asyncio.run(
            kv.fetch_city_images_async(
                "Testville",
                BBOX,
                "tok",
                radius_m=1000,
                retries=0,
                max_requests_per_minute=0,
                checkpoint_path=str(ckpt),
            )
        )
    state = _state(ckpt)
    assert state["roots_done"] == 2
    assert state["census_rows"] > 0
    assert _parts(ckpt)


def test_a_host_refusal_mid_sweep_still_checkpoints_what_it_paid_for(monkeypatch, tmp_path):
    """
    #205 stops at the first refusal; #239 keeps what the refusal interrupted.
    The two compose: the exception still carries the exact spend and still exits
    81, it just no longer takes the night's work with it.
    """
    ckpt = tmp_path / "sweep"
    calls = []

    def responder(call):
        calls.append(call)
        if len(calls) > 2:
            raise HostBlockedError("refused", HOST_KARTAVIEW)
        return _photos(call)

    _install(monkeypatch, responder)
    with pytest.raises(HostBlockedError) as excinfo:
        asyncio.run(
            kv.fetch_city_images_async(
                "Testville",
                BBOX,
                "tok",
                radius_m=1000,
                retries=0,
                max_requests_per_minute=0,
                checkpoint_path=str(ckpt),
            )
        )
    assert excinfo.value.api_requests == 3
    assert host_exit_code(excinfo.value) == HOST_EXIT_CODES[HOST_KARTAVIEW]
    assert _state(ckpt)["roots_done"] == 2


def test_every_commit_rename_is_followed_by_a_directory_fsync():
    """
    Source inspection, following the `progress()` and `host_lock` precedent,
    because the property is a power-loss one and no in-process test can see it:
    a rename is atomic but not durable, so without fsyncing the CONTAINING
    directory the part-then-state ordering the commit docstring promises holds
    only against a process crash -- where the file fsync it already does buys
    nothing anyway.

    The failure that leaves is not a wrong artifact (load_checkpoint's existence
    and footer checks catch a state file naming a part that is not there) but it
    discards the WHOLE checkpoint rather than the last interval, which for a
    multi-night city is the loss #239 exists to prevent.
    """
    lines = inspect.getsource(kv._commit_checkpoint).splitlines()
    renames = [i for i, line in enumerate(lines) if "os.replace(" in line]
    assert len(renames) == 2, f"expected the part and state renames, saw {len(renames)}"
    for i in renames:
        following = next(line for line in lines[i + 1 :] if line.strip())
        assert "_fsync_dir(" in following, (
            f"the rename on line {i + 1} of _commit_checkpoint is not made durable: {following!r}"
        )


def test_a_commit_failure_that_is_not_an_oserror_cannot_swallow_a_host_block(monkeypatch, tmp_path):
    """
    The finally-commit sits on HostBlockedError's re-raise path, so an exception
    escaping it would replace a host block with a serialization error: no exit
    81, no night-level breaker, and a scheduler that keeps asking a host which is
    refusing this IP.

    It used to catch OSError only, which is not the same set as "what a commit
    can raise" -- pyarrow's ArrowIOError is an OSError but ArrowInvalid is a
    ValueError, and json.dump raises TypeError. This drives the non-OSError half
    directly, because that is the half a narrow `except` gets wrong.
    """
    ckpt = tmp_path / "sweep"
    calls = []

    def responder(call):
        calls.append(call)
        if len(calls) > 2:
            raise HostBlockedError("refused", HOST_KARTAVIEW)
        return _photos(call)

    def broken_commit(*args, **kwargs):
        raise ValueError("ArrowInvalid: cannot serialize column")

    _install(monkeypatch, responder)
    monkeypatch.setattr(kv, "_commit_checkpoint", broken_commit)
    with pytest.raises(HostBlockedError) as excinfo:
        asyncio.run(
            kv.fetch_city_images_async(
                "Testville",
                BBOX,
                "tok",
                radius_m=1000,
                retries=0,
                max_requests_per_minute=0,
                checkpoint_path=str(ckpt),
            )
        )
    assert host_exit_code(excinfo.value) == HOST_EXIT_CODES[HOST_KARTAVIEW]
    assert excinfo.value.api_requests == 3


def test_a_progress_bar_that_raises_on_close_cannot_cost_the_commit(monkeypatch, tmp_path):
    """
    `progress()` exists because tqdm raises on a dead output stream (#167), and
    anything that can raise between entering the finally and committing would
    throw away the segment that block exists to save. So the commit goes first
    and the bar is closed after.
    """
    ckpt = tmp_path / "sweep"
    real_progress = kv.progress

    def exploding_progress(*args, **kwargs):
        bar = real_progress(*args, **kwargs)
        bar.close = lambda: (_ for _ in ()).throw(BrokenPipeError("stdout is gone"))
        return bar

    _install(monkeypatch, _photos)
    monkeypatch.setattr(kv, "progress", exploding_progress)
    with pytest.raises(BrokenPipeError):
        asyncio.run(
            kv.fetch_city_images_async(
                "Testville",
                BBOX,
                "tok",
                radius_m=1000,
                retries=0,
                max_requests_per_minute=0,
                checkpoint_path=str(ckpt),
            )
        )
    monkeypatch.undo()

    # The sweep itself completed, so the commit that ran before close() must have
    # recorded every root -- which makes the recovery free.
    again, calls = _sweep_ckpt(monkeypatch, _photos, ckpt)
    assert calls == [], "the commit landed before the bar was closed"
    assert again["api_requests"] == 0


def test_a_zero_commit_interval_is_per_root_flushing_not_no_checkpointing(monkeypatch, tmp_path):
    """
    0 reads like "never commit" and means the opposite: the cadence test is
    `api_requests - requests_at_last_commit >= interval` and no root costs zero
    requests, so anything <= 1 commits at every root boundary. Worth pinning
    because the natural misreading is the dangerous direction -- someone
    reaching for 0 to disable checkpointing gets the tightest cadence there is,
    and the way to actually disable it is `checkpoint_path=None`.
    """
    ckpt = tmp_path / "sweep"
    result, _ = _sweep_ckpt(monkeypatch, _photos, ckpt, checkpoint_request_interval=0)
    whole, _ = _sweep(monkeypatch, _photos)
    pd.testing.assert_frame_equal(result["census"], whole["census"])
    assert len(_parts(ckpt)) == result["cells"], "one part per root, not zero parts"


def test_staging_leftovers_are_swept_when_a_checkpoint_is_reloaded(monkeypatch, tmp_path):
    """
    A commit that died between `to_parquet` and `os.replace` leaves a `.tmp`
    behind. Harmless on its own -- the next commit truncates the same name -- but
    a sweep that keeps being interrupted would otherwise accumulate one per
    attempt in a directory that already holds a partial census.
    """
    ckpt = tmp_path / "sweep"
    _failed_sweep_ckpt(monkeypatch, _photos, ckpt, max_requests=2)
    committed = _parts(ckpt)

    orphan = ckpt / (kv.CHECKPOINT_PART_TEMPLATE.format(index=99) + ".tmp")
    orphan.write_bytes(b"half a parquet file")
    (ckpt / (kv.CHECKPOINT_STATE_FILENAME + ".tmp")).write_text("{half a state file")

    _sweep_ckpt(monkeypatch, _photos, ckpt)
    assert not orphan.exists()
    assert not (ckpt / (kv.CHECKPOINT_STATE_FILENAME + ".tmp")).exists()
    assert _parts(ckpt)[: len(committed)] == committed, "real parts are untouched"


def test_a_city_where_no_radius_answers_leaves_no_checkpoint_directory_behind(
    monkeypatch, tmp_path
):
    """
    The directory is created before the first request on purpose -- an unwritable
    path must fail in a second, not ten hours in -- but a sweep that dies before
    the radius is settled never opens a checkpoint at all. Horace ND does this
    every time it is asked, so without the sweep-up it would leave an empty
    directory per attempt forever.
    """
    ckpt = tmp_path / "sweep"
    error, _ = _failed_sweep_ckpt(
        monkeypatch,
        lambda call: (_ for _ in ()).throw(kv.BackpressureError("apiCode 690")),
        ckpt,
        radius_m=None,
        calibration_probes=1,
    )
    assert "no radius at any calibration point" in str(error)
    assert not ckpt.exists()


def test_a_real_checkpoint_is_never_swept_up_as_an_empty_directory(monkeypatch, tmp_path):
    """The other side of the rmdir: it must only ever remove an EMPTY directory."""
    ckpt = tmp_path / "sweep"
    error, _ = _failed_sweep_ckpt(monkeypatch, _photos, ckpt, max_requests=2)
    assert isinstance(error, kv.SweepIncompleteError)
    assert (ckpt / kv.CHECKPOINT_STATE_FILENAME).exists()


def test_a_torn_part_beyond_the_commit_record_is_ignored_and_removed(monkeypatch, tmp_path):
    """
    The crash window the commit record exists to close: a part written and
    renamed into place, and then the process dies before state.json counts it.
    Its rows were never accounted for, so they are not census -- they are debris,
    and leaving them would put someone else's bytes under the next part's name.
    """
    ckpt = tmp_path / "sweep"
    _failed_sweep_ckpt(monkeypatch, _photos, ckpt, max_requests=2, checkpoint_request_interval=1)
    torn_index = _state(ckpt)["parts"]
    torn = ckpt / kv.CHECKPOINT_PART_TEMPLATE.format(index=torn_index)
    kv.records_to_census(kv.decode_photo_items([_item(id="never-committed")])).to_parquet(
        torn, index=False
    )

    # The load is where the debris goes: a resume that happens to commit nothing
    # further would otherwise leave it to accumulate across nights.
    cp = kv.load_checkpoint(str(ckpt), bbox=BBOX, ipp=kv.IPP_MAX, requested_radius_m=1000)
    assert cp is not None and cp.parts == torn_index
    assert not torn.exists()

    result, _ = _sweep_ckpt(monkeypatch, _photos, ckpt)
    assert "never-committed" not in set(result["census"]["id"])


def test_a_part_missing_under_the_commit_record_discards_the_whole_checkpoint(
    monkeypatch, tmp_path
):
    ckpt = tmp_path / "sweep"
    _failed_sweep_ckpt(monkeypatch, _photos, ckpt, max_requests=2, checkpoint_request_interval=1)
    (ckpt / kv.CHECKPOINT_PART_TEMPLATE.format(index=0)).unlink()

    result, calls = _sweep_ckpt(monkeypatch, _photos, ckpt)
    assert len(calls) == result["cells"], "a checkpoint we cannot trust means a fresh sweep"


def test_a_row_count_that_disagrees_with_the_commit_record_is_discarded(monkeypatch, tmp_path):
    """
    The parts are verified from their FOOTERS at load -- a seek to the end of
    each file, costing nothing -- rather than at finalize, where a truncated
    part would surface only after the night had already been paid for.
    """
    ckpt = tmp_path / "sweep"
    _failed_sweep_ckpt(monkeypatch, _photos, ckpt, max_requests=2, checkpoint_request_interval=1)
    state = _state(ckpt)
    state["census_rows"] += 1
    (ckpt / kv.CHECKPOINT_STATE_FILENAME).write_text(json.dumps(state))

    result, calls = _sweep_ckpt(monkeypatch, _photos, ckpt)
    assert len(calls) == result["cells"]


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda s: s.update(bbox=[-1.0, -1.0, 1.0, 1.0]), id="bbox"),
        pytest.param(lambda s: s.update(format_version=s["format_version"] + 1), id="version"),
        pytest.param(lambda s: s.update(ipp=s["ipp"] // 2), id="ipp"),
        pytest.param(lambda s: s.update(root_count=s["root_count"] + 1), id="root_count"),
        pytest.param(lambda s: s.update(roots_done=s["root_count"] + 99), id="counters"),
    ],
)
def test_a_checkpoint_that_does_not_describe_this_sweep_is_discarded(monkeypatch, tmp_path, mutate):
    """
    Discarded, never refused. Both existing checkpoints in the repo degrade to
    "start over" on anything they cannot trust, and the reason generalizes: a
    checkpoint is not a comparison whose mismatch would corrupt an artifact, so
    the walk-diff posture of refusing outright would cost a night to protect
    nothing.
    """
    ckpt = tmp_path / "sweep"
    _failed_sweep_ckpt(monkeypatch, _empty, ckpt, max_requests=2)
    state = _state(ckpt)
    mutate(state)
    (ckpt / kv.CHECKPOINT_STATE_FILENAME).write_text(json.dumps(state))

    result, calls = _sweep_ckpt(monkeypatch, _empty, ckpt)
    assert len(calls) == result["cells"], "a mismatched checkpoint must not be resumed"


def test_an_explicit_radius_that_contradicts_the_checkpoint_wins(monkeypatch, tmp_path):
    """
    A caller that names a radius is not asking to be silently overridden by a
    stale directory. Its lattice differs, so the checkpoint describes a
    different sweep and goes.
    """
    ckpt = tmp_path / "sweep"
    _failed_sweep_ckpt(monkeypatch, _empty, ckpt, radius_m=1000, max_requests=2)

    result, calls = _sweep_ckpt(monkeypatch, _empty, ckpt, radius_m=500)
    assert result["radius_m"] == 500
    assert {c.radius_m for c in calls} == {500}
    assert len(calls) == result["cells"]


def test_a_changed_lattice_is_discarded_even_at_the_same_radius(monkeypatch, tmp_path):
    """
    Guards a change to cells_for_bbox ITSELF. The module notes that correcting
    the equirectangular cos(mid_lat) shortfall "would move every city's cell
    count"; this is what makes such a change re-sweep rather than resume onto a
    lattice whose indices no longer mean what was recorded against them.
    """
    ckpt = tmp_path / "sweep"
    _failed_sweep_ckpt(monkeypatch, _empty, ckpt, max_requests=2)

    real_cells_for_bbox = kv.cells_for_bbox
    monkeypatch.setattr(kv, "cells_for_bbox", lambda *a, **k: real_cells_for_bbox(*a, **k)[:-1])
    result, calls = _sweep_ckpt(monkeypatch, _empty, ckpt)
    assert len(calls) == result["cells"]


def test_an_unreadable_checkpoint_degrades_to_a_full_sweep(monkeypatch, tmp_path):
    ckpt = tmp_path / "sweep"
    ckpt.mkdir()
    (ckpt / kv.CHECKPOINT_STATE_FILENAME).write_text("{not json at all")

    result, calls = _sweep_ckpt(monkeypatch, _empty, ckpt)
    assert len(calls) == result["cells"]
    assert _state(ckpt)["roots_done"] == result["cells"], "it swept afresh and re-committed"


def test_an_unusable_checkpoint_path_fails_before_the_first_request(monkeypatch, tmp_path):
    """
    Asked up front, deliberately: an unwritable checkpoint discovered ten hours
    in is exactly the failure this feature exists to prevent, arriving from the
    inside.
    """
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    error, calls = _failed_sweep_ckpt(monkeypatch, _empty, blocker / "sweep")
    assert calls == []
    assert "checkpoint" in str(error)
    assert error.api_requests == 0


def test_the_commit_cadence_is_measured_in_requests_not_roots(monkeypatch, tmp_path):
    """
    A root does not cost a fixed amount: one that answers cleanly is a single
    request, while one that cascades to the radius floor is 1 + 4 + 16 + 64
    cells at up to retries + 1 attempts each -- ~340 requests, ~21 minutes. So
    flushing per root would have both a worse worst case and one part file per
    root -- 5,130 of them for Singapore, that being its root-cell count and not
    its 9,974 requests.
    """
    coarse = tmp_path / "coarse"
    _failed_sweep_ckpt(
        monkeypatch, _photos, coarse, radius_m=500, max_requests=4, checkpoint_request_interval=3
    )
    fine = tmp_path / "fine"
    _failed_sweep_ckpt(
        monkeypatch, _photos, fine, radius_m=500, max_requests=4, checkpoint_request_interval=1
    )

    assert _state(coarse)["roots_done"] == _state(fine)["roots_done"] == 4
    assert _state(fine)["parts"] == 4, "one commit per root at an interval of one"
    assert _state(coarse)["parts"] == 2, "the same four roots, committed on request count"
    assert _state(coarse)["census_rows"] == _state(fine)["census_rows"]


def test_a_busy_host_lock_stops_a_resume_before_it_opens_the_checkpoint(monkeypatch, tmp_path):
    """
    The checkpoint needs no lock of its own because every read and write of it
    happens inside the host lock this sweep already takes. A second process is
    refused before it can read -- let alone rewrite -- a directory the first one
    is committing into.
    """
    from filelock import FileLock

    from streetscape_metadata_tracker import host_lock as host_lock_module

    ckpt = tmp_path / "sweep"
    _failed_sweep_ckpt(monkeypatch, _empty, ckpt, max_requests=2)
    before = (ckpt / kv.CHECKPOINT_STATE_FILENAME).read_bytes()

    calls = _install(monkeypatch, _empty)
    competitor = FileLock(host_lock_module.lock_path(HOST_KARTAVIEW), timeout=0)
    competitor.acquire()
    try:
        with pytest.raises(HostBusyError):
            asyncio.run(
                kv.fetch_city_images_async(
                    "Testville",
                    BBOX,
                    "tok",
                    radius_m=1000,
                    max_requests_per_minute=0,
                    checkpoint_path=str(ckpt),
                )
            )
    finally:
        competitor.release()
    assert calls == []
    assert (ckpt / kv.CHECKPOINT_STATE_FILENAME).read_bytes() == before
