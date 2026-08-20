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
import json
import math

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
    assert len(calls) == len(kv.RADIUS_LADDER_M) * 2
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
