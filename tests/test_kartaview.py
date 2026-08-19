"""
KartaView feasibility study (issue #225): scripts/kartaview_probe.py and
scripts/kartaview_shotdate_audit.py.

Network-free tests. Two properties carry the study, and both are the kind that
fail quietly rather than loudly.

(1) THE PAGING INVARIANT. `/1.0/list/nearby-photos/` pages by SEQUENCE, not by
space, so a share computed over a page describes one drive rather than one
neighbourhood. It is trustworthy only where nothing was paged away
(`n_sampled >= total_filtered_items`). This is not hypothetical: the first draft
of docs/experiments/kartaview-feasibility.md reported Seattle at 100% SPHERE
(actually ~8%) and Krabi at 100% null-dated (actually 31%), both because a large
radius returned one long 360 sequence filling the page.

(2) THE CAPTURE-DATE INVARIANT. `shotDate < dateAdded` -- nothing is captured
after it is uploaded. KartaView's v2 endpoint violates it on one 2025 ingest
batch, serving the upload timestamp as the capture date, which no null-check can
catch. Same species as plan_match.plausible_capture_date (#213).

The rest is the committed-record contract from CLAUDE.md ("Notes"): the writeup's
numbers must trace to the committed JSON, and that JSON must be produced by
committed code.
"""

import argparse
import importlib.util
import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(PROJECT_ROOT, "scripts")
DOCS_DIR = os.path.join(PROJECT_ROOT, "docs", "experiments")


def _load(name):
    path = os.path.join(SCRIPTS_DIR, f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


kp = _load("kartaview_probe")
# Imports kartaview_probe by name; the script puts scripts/ on sys.path itself.
ka = _load("kartaview_shotdate_audit")

PROBE_JSON = os.path.join(DOCS_DIR, kp.DOCS_METRICS_NAME)
AUDIT_JSON = os.path.join(DOCS_DIR, "kartaview-shotdate-audit_metrics.json")


@pytest.fixture(scope="module")
def probe():
    with open(PROBE_JSON, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def audit():
    with open(AUDIT_JSON, encoding="utf-8") as fh:
        return json.load(fh)


def is_complete(rung: dict) -> bool:
    """
    The completeness predicate the whole study rests on: the page held every
    matching photo, so its shares describe the circle rather than the page.

    Delegates to the script's own ``is_complete_sample`` rather than restating
    the rule. A second copy here is how a test ends up certifying a predicate the
    producer no longer uses -- and this one governs which numbers the writeup may
    quote at all, so the two must not be able to drift.

    A failed rung is never complete: it has no sample, which is a different thing
    from a sample that happens to be exhaustive.
    """
    if not rung.get("ok"):
        return False
    return kp.is_complete_sample(rung["n_sampled"], rung.get("total_filtered_items"))


def rung(record: dict, target: str, radius_m: int) -> dict:
    (t,) = [t for t in record["targets"] if t["target"] == target]
    (r,) = [r for r in t["per_radius"] if r["radius_m"] == radius_m]
    return r


# ── (1) The paging invariant ────────────────────────────────────────────────


def test_every_target_reaches_a_complete_sample_at_the_server_page_cap(probe):
    """
    The study's central limitation turned out to be a client-side default. At the
    old --ipp 200 only 5 of 8 targets could ever reach a complete sample, and the
    writeup declined to claim any local share for Malioboro, Singapore or NYC on
    that basis. At the documented server cap (2000, the default now) all 8 do --
    for the SAME 48 requests, since a bigger page is the same one request.

    Pinned because it gates which numbers may be quoted at all: a regenerated
    record that LOST completeness must fail here and be re-read, not silently
    shrink the study's conclusions again.
    """
    complete = {t["target"] for t in probe["targets"] if any(map(is_complete, t["per_radius"]))}
    assert complete == {t["target"] for t in probe["targets"]}
    assert len(probe["targets"]) == 8
    assert probe["_about"]["ipp"] == kp.IPP_MAX


def test_a_bigger_page_costs_reach_at_the_large_radii(probe):
    """
    Raising --ipp is a TRADE, not a free win, and the record has to carry both
    halves or the next person repeats the mistake in the other direction. A
    2000-row page is a heavier query, so rungs that answered at 200 now return
    apiCode 690. What it buys is completeness at the radii that matter.
    """
    failed_1000 = [
        t["target"] for t in probe["targets"] if not rung(probe, t["target"], 1000)["ok"]
    ]
    assert len(failed_1000) >= 4, "the backpressure ceiling should still be visible"
    assert all(t["reported_sample_is_complete"] for t in probe["targets"])


def test_the_reported_rung_is_the_largest_complete_one(probe):
    """
    Completeness is the whole paging defence, so among complete rungs a bigger
    circle is strictly more evidence. Preferring the smallest -- an earlier version
    of this rule -- picked Langkawi's r=500 n=1 over its r=1000 n=11, and gave
    Seattle 88 photos instead of 1,534.
    """
    for t in probe["targets"]:
        complete = [r["radius_m"] for r in t["per_radius"] if r.get("complete")]
        assert t["reported_radius_m"] == max(complete), t["target"]
        # A DIFFERENT number, and legitimately larger: the biggest circle the
        # server answers is often one whose page was truncated.
        assert t["max_working_radius_m"] >= t["reported_radius_m"], t["target"]


def test_a_truncated_rung_is_never_promoted_over_a_complete_one(probe):
    """
    The artifact, still in the record and still marked. A truncated page was
    filled by one long sequence, so its share describes a drive rather than a
    neighbourhood -- and every one of them sits above the quotable radius.
    """
    # TRUNCATED means the page was cut short, which is a narrower thing than
    # "not complete": Langkawi's inner rungs are ok, empty and therefore not
    # complete (is_complete_sample requires n_sampled > 0), but nothing was paged
    # away from them. Conflating the two is what this predicate must not do.
    truncated = [
        (t, r)
        for t in probe["targets"]
        for r in t["per_radius"]
        if r.get("ok") and r["total_filtered_items"] and r["n_sampled"] < r["total_filtered_items"]
    ]
    assert truncated, "the record should still contain truncated rungs to warn about"
    for t, r in truncated:
        assert not r["complete"], t["target"]
        assert r["radius_m"] > t["reported_radius_m"], t["target"]

    # An empty rung is neither complete nor truncated -- it observed nothing, and
    # on this API that can equally mean backpressure, so it supports no claim.
    empty = [
        r for t in probe["targets"] for r in t["per_radius"] if r.get("ok") and not r["n_sampled"]
    ]
    assert empty, "Langkawi's inner rungs are the real case"
    assert not any(r["complete"] for r in empty)


def test_writeup_local_360_shares_match_the_record(probe):
    """
    Finding 3's table: each target's largest complete sample. Seattle and NYC are
    the load-bearing pair -- two independent North American points near ~10%
    SPHERE, which is what actually supports the issue's "overwhelmingly flat
    dashcam" claim, where the first draft had one 88-photo sample.
    """
    quoted = {
        "yogyakarta": (300, 1673, 0.48, 43, 12),
        "yogyakarta-malioboro": (300, 1773, 13.54, 49, 8),
        "krabi": (500, 1476, 100.0, 11, 1),
        "seattle": (400, 1534, 11.8, 39, 17),
        "singapore": (200, 1224, 65.69, 18, 4),
        "nyc": (300, 1374, 7.06, 26, 8),
        "langkawi": (1000, 11, 100.0, 3, 1),
        "bucharest": (500, 507, 0.0, 7, 3),
    }
    for target, (radius, n, pct, seqs, users) in quoted.items():
        (t,) = [x for x in probe["targets"] if x["target"] == target]
        assert t["reported_radius_m"] == radius, target
        assert t["reported_sample_is_complete"], target
        got = (t["n_sampled"], t["pct_sphere"], t["distinct_sequences"], t["distinct_usernames"])
        assert got == (n, pct, seqs, users), target


def test_datedness_is_per_sequence_but_not_absolute(probe):
    """
    The claim finding 1 rests on, and the reason this cross-tab is committed
    rather than computed by hand: it is what licenses the audit attributing ONE
    sampled photo's verdict to a drive's whole countActivePhotos.

    Measured across every complete rung it is overwhelmingly true and NOT
    absolute -- 439 of 440 drives are wholly dated or wholly undated, and
    Bucharest sequence 2723 (127 photos, PLANE, a community uploader) is genuinely
    mixed at 14.17% dated. An earlier draft asserted "never mixed" from a 7-row
    hand-made table; a larger sample found the counterexample on its first run.
    The extrapolation therefore stands for the Grab batch -- one SPHERE ingest,
    checked below -- and must never be restated as a universal law.
    """
    rows = [
        s
        for t in probe["targets"]
        for r in t["per_radius"]
        if r.get("complete")
        for s in r["per_sequence"]
    ]
    assert len(rows) == 440
    mixed = [s for s in rows if not s["datedness_is_absolute"]]
    assert [s["sequence_id"] for s in mixed] == ["2723"]
    assert mixed[0]["pct_dated"] == 14.17
    assert {s["pct_dated"] for s in rows if s["datedness_is_absolute"]} == {0.0, 100.0}


def _drives(probe):
    """One row per distinct drive across complete rungs, largest observation kept."""
    best: dict[str, dict] = {}
    for t in probe["targets"]:
        for r in t["per_radius"]:
            if not r.get("complete"):
                continue
            for s in r["per_sequence"]:
                cur = best.get(s["sequence_id"])
                if cur is None or s["n_on_page"] > cur["n_on_page"]:
                    best[s["sequence_id"]] = {**s, "target": t["target"]}
    return list(best.values())


def test_undated_imagery_is_TWO_populations_not_just_the_grab_batch(probe):
    """
    A correction the complete samples forced, and the reason the old reading was
    wrong rather than merely narrow.

    The first draft said "the only undated sequences are Grab's 2025-11 bulk
    upload (ids 1161...)", which made missing dates look like a single fixable
    ingest. It was an artifact of where the old sample could see: Seattle's only
    complete rung held 88 photos / 12 drives and read 0% null, so the community
    half was invisible. At r=400 Seattle holds 1,534 photos / 39 drives and reads
    8.87% null.

    Measured over every complete rung: 19 wholly-undated drives, of which 12 ARE
    the Grab batch (all SPHERE, uploader OpenStreetView) and 7 are ORDINARY
    COMMUNITY uploads (all PLANE, four different uploaders, three cities). Two
    different problems wearing one symptom: a systematic fleet-ingest regression,
    and everyday missing EXIF. A collector must handle both, and only the first
    is plausibly temporary.
    """
    undated = [s for s in _drives(probe) if s["pct_dated"] == 0.0]
    grab = [s for s in undated if s["sequence_id"].startswith("1161")]
    community = [s for s in undated if not s["sequence_id"].startswith("1161")]

    assert (len(undated), len(grab), len(community)) == (19, 12, 7)
    # The two populations are cleanly separated by projection and uploader...
    assert {p for s in grab for p in s["projections"]} == {"SPHERE"}
    assert {u for s in grab for u in s["usernames"]} == {"OpenStreetView"}
    assert {p for s in community for p in s["projections"]} == {"PLANE"}
    assert len({u for s in community for u in s["usernames"]}) == 4
    assert len({s["target"] for s in community}) == 3
    # ...so "OpenStreetView" is not shorthand for "undated" either way.
    assert "OpenStreetView" not in {u for s in community for u in s["usernames"]}


def test_a_dated_360_drive_exists_so_sphere_does_not_mean_undated(probe):
    """
    The claim that survives, and the one that keeps KartaView usable: 360 imagery
    is not inherently dateless. 29 wholly-dated SPHERE drives sit in the same
    record as the 12 undated ones, so the defect is scoped to an ingest rather
    than to the projection or to the fleet that shoots it.
    """
    dated_sphere = [
        s for s in _drives(probe) if s["pct_dated"] == 100.0 and s["projections"] == ["SPHERE"]
    ]
    assert len(dated_sphere) == 29


# ── _summarize: capture years and upload years stay apart ───────────────────


def _item(projection="SPHERE", shot=None, added=None, seq="1", user="u"):
    return {
        "projection": projection,
        "shot_date": shot,
        "date_added": added,
        "sequence_id": seq,
        "username": user,
    }


def test_summarize_never_merges_upload_years_into_capture_years():
    """
    A `shot_date or date_added` fallback would file Krabi's undated 2025 bulk
    upload beside Seattle's genuine 2025 capture year, indistinguishably. The
    two tallies are what make the fallback visible instead of silent.
    """
    items = [
        _item(shot="2023-03-18 07:00:00", added="2023-11-23 14:00:00"),
        _item(shot=None, added="2025-11-19 11:00:00"),
        _item(shot=None, added="2025-11-19 11:00:01"),
    ]
    s = kp._summarize(items)
    assert s["capture_year_counts_shot_date"] == {"2023": 1}
    assert s["upload_year_counts_date_added"] == {"2023": 1, "2025": 2}
    assert s["shot_date_null"] == 2
    assert s["pct_shot_date_null"] == pytest.approx(66.67, abs=0.01)


def test_summarize_shares_are_over_the_sampled_page_not_the_city():
    """
    n_sampled is the denominator of every percentage in the record, which is
    precisely why completeness has to be checked beside it.
    """
    s = kp._summarize([_item(), _item(projection="PLANE"), _item(projection=None)])
    assert s["n_sampled"] == 3
    assert s["pct_sphere"] == pytest.approx(33.33, abs=0.01)
    assert s["projection_counts"]["UNKNOWN"] == 1


def test_summarize_of_an_empty_page_reports_no_share_rather_than_zero():
    """
    0% SPHERE and "we saw nothing" are different claims; Langkawi's empty radii
    are the real case. A zero here would read as "imagery present, none of it
    360".
    """
    s = kp._summarize([])
    assert s["n_sampled"] == 0
    assert s["pct_sphere"] is None
    assert s["pct_shot_date_null"] is None


# ── (2) The capture-date invariant ──────────────────────────────────────────


def test_classify_rejects_a_capture_at_or_after_its_own_upload():
    """
    `>=`, not `>`. Three of the ten violating sequences read shotDate ==
    dateAdded to the second (Langkawi 11616157 is 2025-11-19 11:18:29 on both),
    so a strict `>` would pass exactly the near-miss records the predicate
    exists to catch.
    """
    assert ka.classify("2025-11-19 11:18:29", "2025-11-19 11:18:29") == "invalid"
    assert ka.classify("2025-11-19 11:16:25", "2025-11-19 11:16:24") == "invalid"
    assert ka.classify("2023-03-19 07:12:58.000", "2023-11-23 14:40:04") == "ok"


def test_classify_is_unknown_when_a_side_of_the_comparison_is_missing():
    """An unanswerable comparison must never default to 'ok'."""
    assert ka.classify(None, "2025-11-19 11:18:29") == "unknown"
    assert ka.classify("2025-11-19 11:18:29", None) == "unknown"
    assert ka.classify(None, None) == "unknown"


def test_classify_ignores_sub_second_precision_differences():
    """
    v1 renders milliseconds and v2 does not, so a lexical compare of the raw
    strings would flip on formatting rather than on time.
    """
    assert ka.classify("2025-11-19 11:18:29.000", "2025-11-19 11:18:29") == "invalid"


def test_summarize_counts_whole_sequences_not_the_sampled_page():
    """
    photos_invalid is the record's headline number and its stated meaning is
    countActivePhotos -- the sequence's full size. Counting sampled rows instead
    would under-report a 3,242-photo drive as 1.
    """
    rows = [
        {
            "verdict": "invalid",
            "city": "Yogyakarta",
            "device": "KartaCam2",
            "count_active_photos": 3242,
            "sequence_date_added": "2025-11-19 10:54:07",
        },
        {
            "verdict": "ok",
            "city": "Seattle",
            "device": "GoPro Max",
            "count_active_photos": 7,
            "sequence_date_added": "2025-09-13 00:00:00",
        },
    ]
    s = ka.summarize(rows)
    assert s["sequences_audited"] == 2
    assert s["photos_audited"] == 3249
    assert (s["sequences_invalid"], s["photos_invalid"]) == (1, 3242)
    assert s["by_city"]["Yogyakarta"] == {"sequences": 1, "photos": 3242}
    assert s["invalid_upload_dates"] == ["2025-11-19"]
    assert s["invalid_devices"] == ["KartaCam2"]


def test_summarize_tolerates_a_sequence_with_no_photo_count():
    """A missing countActivePhotos must not abort the audit or count as a photo."""
    s = ka.summarize([{"verdict": "ok", "city": "X", "device": "d", "count_active_photos": None}])
    assert s["photos_audited"] == 0


# ── The committed audit: scope of the defect ───────────────────────────────


def test_committed_audit_summary_recomputes_from_its_own_rows(audit):
    assert ka.summarize(audit["sequences"]) == audit["summary"]


def test_writeup_audit_totals_match_the_record(audit):
    """Finding 1's table."""
    s = audit["summary"]
    assert (s["sequences_audited"], s["photos_audited"]) == (48, 59263)
    assert (s["sequences_invalid"], s["photos_invalid"]) == (10, 5665)


def test_a_null_v1_date_and_an_invalid_v2_date_name_the_same_sequences(audit):
    """
    The two endpoints disagree about what they SAY and agree exactly about which
    drives are affected. That equivalence is what licenses the writeup's claim
    that this is one ingest rather than two unrelated defects -- and it is why a
    collector cannot pick the "better" endpoint to dodge the problem.
    """
    v1_null = {r["sequence_id"] for r in audit["sequences"] if r["v1_shot_date"] is None}
    v2_invalid = {r["sequence_id"] for r in audit["sequences"] if r["verdict"] == "invalid"}
    assert v1_null == v2_invalid
    assert len(v1_null) == 10


def test_every_violating_sequence_is_the_one_2025_grab_batch(audit):
    """
    The scope claim, pinned in every attribute the writeup asserts it in. A
    re-run that found the defect in a community sequence, a PLANE sequence or a
    different upload date has found something new, and must not slip through as
    a refresh of this record.
    """
    invalid = [r for r in audit["sequences"] if r["verdict"] == "invalid"]
    assert {r["username"] for r in invalid} == {"OpenStreetView"}
    assert {r["user_id"] for r in invalid} == {"44"}
    assert {r["projection"] for r in invalid} == {"SPHERE"}
    assert {r["device"] for r in invalid} == {"KartaCam2"}
    assert {r["sequence_date_added"][:10] for r in invalid} == {"2025-11-19"}
    assert all(r["sequence_id"].startswith("1161") for r in invalid)
    assert audit["summary"]["by_city"].keys() >= {"Krabi", "Yogyakarta", "Langkawi"}


def test_the_grab_market_and_community_control_points_are_clean(audit):
    """
    What makes "one bad ingest" a measurement rather than a guess: the audit
    deliberately sampled Grab markets outside the open release, plus a
    pure-community city, and found the defect in neither.
    """
    invalid_cities = {r["city"] for r in audit["sequences"] if r["verdict"] == "invalid"}
    assert invalid_cities == {"Krabi", "Yogyakarta", "Langkawi"}
    for control in ("Singapore", "Bangkok", "Ho Chi Minh City", "Seattle"):
        assert control in audit["summary"]["by_city"], f"{control} was not actually audited"
        assert control not in invalid_cities


def test_the_equality_cases_are_a_real_share_of_the_defect(audit):
    """
    The `>=`-not-`>` decision, priced. If these were a rounding curiosity the
    strict predicate would be defensible; they are 3 of 10 sequences.
    """
    equal = [
        r
        for r in audit["sequences"]
        if r["verdict"] == "invalid" and r["v2_shot_date"][:19] == r["v2_date_added"][:19]
    ]
    assert len(equal) == 3
    assert sum(r["count_active_photos"] for r in equal) == 185


# ── Provenance and the collection-host refusal ─────────────────────────────


def test_committed_records_name_their_producers(probe, audit):
    assert probe["_about"]["generated_by"] == (
        "scripts/kartaview_probe.py --area all --all-radii --docs-dir docs/experiments"
    )
    assert audit["_about"]["generated_by"] == (
        "scripts/kartaview_shotdate_audit.py --docs-dir docs/experiments"
    )
    for rec in (probe, audit):
        assert rec["_about"]["writeup"] == "docs/experiments/kartaview-feasibility.md"
        assert rec["_about"]["issue"] == 225


def test_generated_by_names_the_invocation_not_a_constant():
    """
    A fixed stamp would let a scratch run write a record claiming the canonical
    provenance. Every flag that changes the record's CONTENT has to show up.
    """
    canonical = kp.docs_generated_by(
        argparse.Namespace(
            area="all", all_radii=True, ipp=kp.IPP_MAX, repeat=1, docs_dir="docs/experiments"
        )
    )
    assert canonical == (
        "scripts/kartaview_probe.py --area all --all-radii --docs-dir docs/experiments"
    )
    scratch = kp.docs_generated_by(
        argparse.Namespace(area="krabi", all_radii=False, ipp=50, repeat=4, docs_dir="/tmp/scratch")
    )
    assert scratch != canonical
    for fragment in ("--area krabi", "--ipp 50", "--repeat 4", "/tmp/scratch"):
        assert fragment in scratch
    assert "--all-radii" not in scratch

    # The page size is elided only at the DEFAULT, and the default is the server
    # cap. A run at the old 200 must therefore say so: it is the flag that decides
    # whether a circle can reach a complete sample at all, so a record that hid it
    # would let two incomparable studies claim one provenance string.
    narrow = kp.docs_generated_by(
        argparse.Namespace(area="all", all_radii=True, ipp=200, repeat=1, docs_dir="d")
    )
    assert "--ipp 200" in narrow


def test_the_probe_refuses_to_run_on_a_production_collection_host(monkeypatch):
    """
    A probe exists to find a provider's limits by poking at them, and both prior
    per-IP bans (Mapillary tiles #198, Overpass #209) landed on makelab2 and took
    out every channel on the box. The audit shares this guard by importing it.
    """
    monkeypatch.setattr(kp.socket, "gethostname", lambda: "makelab2.cs.washington.edu")
    with pytest.raises(SystemExit) as exc:
        kp.refuse_on_collection_host()
    assert "makelab2" in str(exc.value)

    monkeypatch.setattr(kp.socket, "gethostname", lambda: "Jons-MacBook-Pro.local")
    kp.refuse_on_collection_host()


def test_the_audit_reuses_the_probes_refusal_rather_than_copying_it():
    assert ka.refuse_on_collection_host is kp.refuse_on_collection_host


# ── _post_nearby: the parse the whole completeness gate depends on ──────────


class _FakeResponse:
    def __init__(self, payload, status_code=200, ctype="application/json", text=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = {"Content-Type": ctype}
        self._text = text

    def json(self):
        if self._text is not None:
            raise ValueError("not json")
        return self._payload


class _FakeSession:
    """Records the last request and serves a canned response. No network."""

    def __init__(self, response):
        self._response = response
        self.last = None

    def post(self, url, data=None, params=None, timeout=None):
        self.last = {"url": url, "data": data, "params": params}
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class _NoPace:
    def acquire(self):
        return None


def _post(response, **kw):
    return kp._post_nearby(_FakeSession(response), _NoPace(), 1.0, 2.0, 100, **kw)


def test_total_filtered_items_arrives_list_wrapped_and_is_unwrapped():
    """
    MEASURED: the API returns this as ['737'], not 737. A bare int() raises on
    that, and the tempting fallback -- len(items) -- reports the PAGE SIZE as the
    city total, i.e. 5 instead of 737. That is the difference between "almost no
    imagery here" and "this city is dense", wrong in the direction that reads as a
    negative result, and `is_complete_sample` consumes it.
    """
    items, total = _post(
        _FakeResponse({"currentPageItems": [{}] * 5, "totalFilteredItems": ["737"]})
    )
    assert (len(items), total) == (5, 737)


def test_an_unparseable_total_is_unknown_rather_than_the_page_size():
    items, total = _post(_FakeResponse({"currentPageItems": [{}] * 5, "totalFilteredItems": "??"}))
    assert (len(items), total) == (5, None)
    # None must never be read as complete -- that is the fail-open direction.
    assert kp.is_complete_sample(5, total) is False


def test_the_osv_wrapped_envelope_is_accepted():
    """The v1 envelope varies across deployments; a server that answered well
    must not read as a parse failure."""
    body = {"osv": {"currentPageItems": [{}] * 3, "totalFilteredItems": 3}}
    items, total = _post(_FakeResponse(body))
    assert (len(items), total) == (3, 3)


def test_backpressure_raises_rather_than_returning_an_empty_page():
    """
    apiCode 690/408 inside an HTTP 400 means "shrink the query", not "no imagery".
    Returning [] here would let a refused dense city be recorded as a complete
    zero -- the conflation the module docstring forbids.
    """
    body = {"status": {"apiCode": 690, "apiMessage": "server error"}}
    with pytest.raises(kp.ProbeError, match="backpressure"):
        _post(_FakeResponse(body, status_code=400))


def test_an_html_error_page_is_named_rather_than_parsed():
    with pytest.raises(kp.ProbeError, match="non-JSON"):
        _post(_FakeResponse(None, ctype="text/html", text="<html>nope</html>"))


def test_a_missing_items_key_is_an_error_not_an_empty_result():
    with pytest.raises(kp.ProbeError, match="currentPageItems"):
        _post(_FakeResponse({"totalFilteredItems": 5}))


# ── The credential must not reach a committed record ────────────────────────


def test_a_transport_failure_cannot_leak_the_token_into_the_record(tmp_path):
    """
    access_token travels as a query param and requests exceptions stringify with
    the full URL, so the raw text goes into per_radius[].error -- which is written
    to a git-tracked file in a public repo. Same hazard, same helper and same
    posture as tests/test_credential_redaction.py.
    """
    secret = "SUPERSECRETKARTAVIEWTOKEN"
    boom = kp.requests.ConnectionError(
        f"HTTPSConnectionPool(host='kartaview.org', port=443): Max retries exceeded "
        f"with url: /1.0/list/nearby-photos/?access_token={secret}"
    )
    with pytest.raises(kp.ProbeError) as exc:
        _post(boom, access_token=secret)
    assert secret not in str(exc.value)
    assert "access_token=REDACTED" in str(exc.value)

    # ...and end to end, through the object that actually gets serialized.
    target = kp.probe_target(
        _FakeSession(boom), _NoPace(), "t", 1.0, 2.0, ipp=200, access_token=secret, all_radii=True
    )
    assert secret not in json.dumps(target)
    assert target["max_working_radius_m"] is None
    assert all(not r["ok"] for r in target["per_radius"])


# ── Completeness: an empty page is not an exhaustive sample ─────────────────


def test_a_rung_that_saw_nothing_is_not_a_complete_sample():
    """
    0 >= 0 is True, so a naive predicate scores a rung that observed NOTHING as
    exhaustive. This API answers an overloaded query with backpressure and can
    return an empty page, so that would turn a refused dense city into a
    confident 0% -- contradicting
    test_summarize_of_an_empty_page_reports_no_share_rather_than_zero.
    """
    assert kp.is_complete_sample(0, 0) is False
    assert kp.is_complete_sample(11, 11) is True
    assert kp.is_complete_sample(200, 274) is False
    assert kp.is_complete_sample(5, None) is False


def test_the_headline_is_the_largest_complete_rung_not_the_largest_working_one():
    """
    The record's most-read field used to hold the largest WORKING rung, i.e. the
    paged-away one -- it read Seattle 100% SPHERE and Krabi 100% null-dated, the
    exact two numbers this study had to retract. It is now the largest COMPLETE
    rung: completeness is the whole paging defence, so among complete rungs a
    bigger circle is strictly more evidence. max_working_radius_m stays beside it
    as a separate COST measurement.
    """
    pages = {
        1000: ({"currentPageItems": [{"projection": "SPHERE"}] * 200, "totalFilteredItems": 2030}),
        100: ({"currentPageItems": [{"projection": "PLANE"}] * 88, "totalFilteredItems": 88}),
    }

    class _Ladder(_FakeSession):
        def post(self, url, data=None, params=None, timeout=None):
            body = pages.get(data["radius"], {"currentPageItems": [], "totalFilteredItems": 0})
            return _FakeResponse(body)

    t = kp.probe_target(_Ladder(None), _NoPace(), "seattle", 1.0, 2.0, 2000, None, all_radii=True)
    assert t["max_working_radius_m"] == 1000  # largest circle the server answered
    assert t["reported_radius_m"] == 100  # the only complete rung here
    assert t["reported_sample_is_complete"] is True
    assert t["pct_sphere"] == 0.0  # the complete rung's share, not the artifact's


# ── The per-sequence cross-tab (finding 1's table, now committed) ───────────


def test_per_sequence_reports_datedness_per_drive():
    """
    The claim this table exists to support: datedness is decided per SEQUENCE and
    is absolute, which is what licenses the audit attributing one sampled photo's
    verdict to the drive's whole countActivePhotos.
    """
    items = [
        _item(seq="dated", shot="2023-03-18 07:00:00", added="2023-11-23 14:00:00"),
        _item(seq="dated", shot="2023-03-19 07:00:00", added="2023-11-23 14:00:00"),
        _item(seq="undated", shot=None, added="2025-11-19 11:00:00", user="OpenStreetView"),
        _item(seq="mixed", shot="2020-01-01 00:00:00", added="2020-02-01 00:00:00"),
        _item(seq="mixed", shot=None, added="2020-02-01 00:00:00"),
    ]
    by_id = {r["sequence_id"]: r for r in kp._per_sequence(items)}
    assert by_id["dated"]["pct_dated"] == 100.0
    assert by_id["undated"]["pct_dated"] == 0.0
    assert by_id["dated"]["datedness_is_absolute"] is True
    assert by_id["undated"]["datedness_is_absolute"] is True
    # A half-dated drive would falsify the claim, so it is flagged, not averaged.
    assert by_id["mixed"]["datedness_is_absolute"] is False
    assert by_id["dated"]["shot_date_range"] == ["2023-03-18", "2023-03-19"]
    assert by_id["undated"]["shot_date_range"] is None


# ── classify: compared as time, not as text ────────────────────────────────


def test_classify_is_not_fooled_by_a_different_timestamp_rendering():
    """
    A lexical compare works only while both fields render identically. 'T' (0x54)
    sorts above ' ' (0x20), so an ISO-8601 shotDate against a space-separated
    dateAdded would flip EVERY sequence to invalid at once.
    """
    assert ka.classify("2023-03-19T07:12:58", "2023-11-23 14:40:04") == "ok"
    assert ka.classify("2025-11-19T11:18:29", "2025-11-19 11:18:29") == "invalid"


def test_classify_treats_a_mysql_zero_date_as_unknown_not_ok():
    """
    '0000-00-00 00:00:00' is not a time. Plausible here: the writeup cites a MySQL
    collation error leaking out of findNearbyPhotos, the function a collector
    calls. Lexically it sorts below everything and would read as a clean 'ok'.
    """
    assert ka.classify("0000-00-00 00:00:00", "2023-11-23 14:40:04") == "unknown"


def test_classify_takes_only_v2s_own_two_fields():
    """It used to accept a v1 date it never read, which made it look like a
    two-endpoint cross-check. That correspondence is asserted over the record."""
    import inspect

    assert list(inspect.signature(ka.classify).parameters) == ["v2_shot", "v2_added"]
