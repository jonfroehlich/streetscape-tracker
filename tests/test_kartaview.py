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

    A failed rung is never complete -- it has no sample at all, which is a
    different thing from a sample that happens to be exhaustive.
    """
    if not rung.get("ok"):
        return False
    total = rung.get("total_filtered_items")
    return total is not None and rung["n_sampled"] >= total


def rung(record: dict, target: str, radius_m: int) -> dict:
    (t,) = [t for t in record["targets"] if t["target"] == target]
    (r,) = [r for r in t["per_radius"] if r["radius_m"] == radius_m]
    return r


# ── (1) The paging invariant ────────────────────────────────────────────────


def test_only_five_of_eight_targets_ever_reach_a_complete_sample(probe):
    """
    The writeup's "5 of 8 probe points" gate. Everything it declines to claim --
    no local share for Malioboro, Singapore or NYC -- rests on this count, so a
    regenerated record that quietly gained completeness must fail here and be
    re-read rather than silently widen the study's conclusions.
    """
    complete = {t["target"] for t in probe["targets"] if any(map(is_complete, t["per_radius"]))}
    assert complete == {"yogyakarta", "krabi", "seattle", "langkawi", "bucharest"}
    assert len(probe["targets"]) == 8


def test_the_sphere_share_rises_as_the_sample_gets_less_complete(probe):
    """
    The artifact itself, at the point where it is starkest. A genuine spatial
    sample would show MORE drives in a bigger circle; this shows fewer, because
    one long 360 sequence fills the page. Pinning it keeps a future reader from
    quoting the r=500 figure as Seattle's 360 share.
    """
    ladder = [rung(probe, "seattle", r) for r in (100, 200, 300, 400, 500)]
    assert [r["distinct_sequences"] for r in ladder] == [12, 10, 6, 2, 1]
    assert [r["pct_sphere"] for r in ladder] == [7.95, 20.5, 54.0, 90.5, 100.0]
    # Only the smallest radius is a complete sample; the 100% is the artifact.
    assert [is_complete(r) for r in ladder] == [True, False, False, False, False]


def test_an_incomplete_rung_is_recorded_with_its_own_total(probe):
    """
    Completeness has to be checkable per rung from the record alone -- that is
    what `--all-radii` buys. A rung that dropped total_filtered_items would make
    every share on it unfalsifiable rather than merely wrong.
    """
    for target in probe["targets"]:
        for r in target["per_radius"]:
            if not r.get("ok"):
                assert r.get("error"), f"{target['target']} r={r['radius_m']} failed with no reason"
                continue
            assert "n_sampled" in r and "total_filtered_items" in r


def test_a_failed_rung_is_not_read_as_an_exhaustive_sample(probe):
    """
    Backpressure (apiCode 690/408 inside an HTTP 400) means "shrink the query",
    not "no imagery here". Counting a failure as complete would turn every dense
    city into a spurious 0.
    """
    failed = [r for t in probe["targets"] for r in t["per_radius"] if not r.get("ok")]
    assert failed, "the record should still contain the r=1000 backpressure failures"
    assert not any(map(is_complete, failed))


# ── The writeup's numbers, recomputed from the committed record ─────────────


def test_writeup_complete_sample_table_matches_the_record(probe):
    """Finding 3's table -- the only local shares the study actually claims."""
    quoted = {
        ("seattle", 100): (88, 7.95, 9),
        ("yogyakarta", 100): (163, 0.0, 4),
        ("krabi", 100): (125, 100.0, 1),
        ("bucharest", 100): (21, 0.0, 1),
        ("bucharest", 200): (67, 0.0, 2),
        ("bucharest", 300): (145, 0.0, 2),
        ("langkawi", 1000): (11, 100.0, 1),
    }
    for (target, radius), (n, pct, uploaders) in quoted.items():
        r = rung(probe, target, radius)
        assert is_complete(r), f"{target} r={radius} is quoted but is not a complete sample"
        assert (r["n_sampled"], r["pct_sphere"], r["distinct_usernames"]) == (n, pct, uploaders)


def test_writeup_seattle_uploader_ladder_matches_the_record(probe):
    """The paging table's third row: uploaders collapse with the sequences."""
    assert [rung(probe, "seattle", r)["distinct_usernames"] for r in (100, 200, 300, 400, 500)] == [
        9,
        5,
        4,
        2,
        1,
    ]


def test_writeup_krabi_null_date_share_is_the_complete_sample(probe):
    """
    Finding 1 quotes 31.2% null at Krabi. The larger radii read 90-100% null --
    the artifact the first draft published as a fact.
    """
    assert rung(probe, "krabi", 100)["pct_shot_date_null"] == 31.2
    assert rung(probe, "krabi", 1000)["pct_shot_date_null"] == 100.0
    assert not is_complete(rung(probe, "krabi", 1000))


def test_writeup_bucharest_is_zero_percent_360_at_every_complete_radius(probe):
    """
    Finding 7's answer to the issue's "dense legacy Telenav" claim: the imagery
    is there and none of it is 360. Three independent complete samples say so,
    which is why this one survives the paging caveat.
    """
    (bucharest,) = [t for t in probe["targets"] if t["target"] == "bucharest"]
    complete = [r for r in bucharest["per_radius"] if is_complete(r)]
    assert len(complete) == 3
    assert {r["pct_sphere"] for r in complete} == {0.0}


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
    assert ka.classify(None, "2025-11-19 11:18:29", "2025-11-19 11:18:29") == "invalid"
    assert ka.classify(None, "2025-11-19 11:16:25", "2025-11-19 11:16:24") == "invalid"
    assert ka.classify(None, "2023-03-19 07:12:58.000", "2023-11-23 14:40:04") == "ok"


def test_classify_is_unknown_when_a_side_of_the_comparison_is_missing():
    """An unanswerable comparison must never default to 'ok'."""
    assert ka.classify(None, None, "2025-11-19 11:18:29") == "unknown"
    assert ka.classify(None, "2025-11-19 11:18:29", None) == "unknown"
    assert ka.classify("2023-03-19 07:12:58", None, None) == "unknown"


def test_classify_ignores_sub_second_precision_differences():
    """
    v1 renders milliseconds and v2 does not, so a lexical compare of the raw
    strings would flip on formatting rather than on time.
    """
    assert ka.classify(None, "2025-11-19 11:18:29.000", "2025-11-19 11:18:29") == "invalid"


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
            area="all", all_radii=True, ipp=200, repeat=1, docs_dir="docs/experiments"
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
