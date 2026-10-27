"""scripts/mapillary_night_report.py: the #444 Stage 1 read-out (read-only).

The module-level fixtures (``DONE_*``, ``FILL_ROOM_1026``, ``STATUS_1023``)
are copied VERBATIM from the read-only prod pull of 2026-10-27 -- including
the pull's truncation of the longer ``Done:`` lines, which is why the 10-22
line ends mid-number. A parser that only works on a tidied-up line is a parser
for a log nobody writes.

The lines built inside individual tests, and ``STRANDED_1026``, are NOT from
the pull, and each says so: the pull holds no stranded line, no wind-down
line, no start line and no two-host refusal, so those are written in the
scheduler's own format (the f-string each names) with values the scheduler
can actually produce.
"""

import glob
import os
import sqlite3
from datetime import date

import pytest

from scripts import mapillary_night_report as report
from streetscape_metadata_tracker import db

DONE_1016 = (
    "2026-10-16 06:30:19,488 - streetscape_scheduler - INFO - Done: run-due 2026-10-16: "
    "34/34 runs succeeded across 20 cities (20 due, 0 fill) in 4.25 h; Mapillary's tile CDN "
    "(tiles.mapillary.com) unavailable, 2 launch(es) skipped while latched; fill (early "
    "refresh, >= 30 d): 0 cities, 0/0 runs, 0 early refresh(es) recorded; declined 351 for "
    "backlog; holding mapillary (3 due not attempted); reserved 1,"
)
DONE_1022 = (
    "2026-10-22 04:07:16,909 - streetscape_scheduler - INFO - Done: run-due 2026-10-22: "
    "65/189 runs succeeded across 80 cities in 2.09 h; 20 deferred for the rolling-24h budget "
    "of Mapillary's tile CDN (tiles.mapillary.com); on the KartaView API (kartaview.org) "
    "09:03-11:07 UTC (2.1 h), 165.3 h after its previous ledgered request; on Mapillary's "
    "tile CDN (tiles.mapillary.com) 09:01-10:06 UTC (1.1 h), 120"
)
DONE_1025 = (
    "2026-10-25 04:37:15,768 - streetscape_scheduler - INFO - Done: run-due 2026-10-25: "
    "160/160 runs succeeded across 80 cities in 2.46 h; HELD Mapillary's tile CDN "
    "(tiles.mapillary.com): rest night (Sun), 0 launch(es) held, cities stay due; on the "
    "Panoramax meta-catalog (api.panoramax.xyz) 09:09-11:37 UTC (2.5 h), 21.6 h after its "
    "previous ledgered request; fill not reached (the due slate ended the ni"
)
DONE_1026 = (
    "2026-10-26 03:06:46,476 - streetscape_scheduler - INFO - Done: run-due 2026-10-26: "
    "112/112 runs succeeded across 38 cities (20 due, 18 fill) in 1.02 h; on Mapillary's "
    "tile CDN (tiles.mapillary.com) 09:39-10:06 UTC (0.5 h), 48.2 h after its previous "
    "ledgered request; on the Panoramax meta-catalog (api.panoramax.xyz) 09:05-09:37 UTC "
    "(0.5 h), 21.5 h after its previous ledgered request; fill (early re"
)
FILL_ROOM_1026 = [
    "2026-10-26 03:00:43,811 - streetscape_scheduler - INFO - waltham--massachusetts--"
    "united-states [fill]: declined — ~72 req on Mapillary's tile CDN (tiles.mapillary.com) "
    "does not fit its fill room (69)",
    "2026-10-26 03:06:45,994 - streetscape_scheduler - INFO - potlatch--idaho--united-states "
    "[fill]: declined — ~8 req on Mapillary's tile CDN (tiles.mapillary.com) does not fit "
    "its fill room (5)",
]
STATUS_1023 = {
    "charleston--south-carolina--united-states": (
        "2026-10-23 02:12:19 - streetscape_metadata_tracker.download_mapillary - INFO - "
        "mapillary tile statuses: 200=26 204=0 404=0 429=0 4xx=0 5xx=0 3xx=0 other=0 noresp=0 "
        "requests=26 retries=0 retry_after=[] city='Charleston, South Carolina, United States'"
    ),
    "bendigo--victoria--australia": (
        "2026-10-23 02:49:41 - streetscape_metadata_tracker.download_mapillary - INFO - "
        "mapillary tile statuses: 200=163 204=0 404=0 429=0 4xx=0 5xx=0 3xx=0 other=0 noresp=0 "
        "requests=163 retries=0 retry_after=[] city='Bendigo, Victoria, Australia'"
    ),
    "cheyenne--wyoming--united-states": (
        "2026-10-23 02:52:58 - streetscape_metadata_tracker.download_mapillary - INFO - "
        "mapillary tile statuses: 200=62 204=0 404=0 429=0 4xx=0 5xx=0 3xx=0 other=0 noresp=0 "
        "requests=62 retries=0 retry_after=[] city='Cheyenne, Wyoming, United States'"
    ),
}


def _register(conn, name):
    return db.register_city(
        conn,
        city_name=name,
        state_name="Oregon",
        state_code="OR",
        country_name="United States",
        country_code="US",
        center_lat=44.0,
        center_lon=-121.0,
        grid_width_m=1000,
        grid_height_m=1000,
        step_m=20,
    )


def _run_row(conn, city_id, run_date, api_requests, provider="mapillary"):
    conn.execute(
        "INSERT INTO runs (city_id, provider, run_date, csv_filename, api_requests) "
        "VALUES (?, ?, ?, ?, ?)",
        (city_id, provider, run_date, f"{city_id}_{provider}_{run_date}.csv.gz", api_requests),
    )


@pytest.fixture
def prod(tmp_path):
    """A catalog, a log dir and a scheduler TOML pointing at both."""
    data_dir = tmp_path / "data"
    log_dir = tmp_path / "logs"
    data_dir.mkdir()
    log_dir.mkdir()
    db_path = data_dir / "streetscape_tracker.db"
    conn = db.connect(str(db_path))
    seed = _register(conn, "Seed")
    _run_row(conn, seed, "2026-10-01", 1, provider="gsv")
    conn.commit()
    cfg = tmp_path / "scheduler.toml"
    cfg.write_text(
        "[schedule]\ncycle_days = 90\ngrace_days = 7\nmax_cities_per_day = 80\n\n"
        "[providers.gsv]\nenabled = true\n\n"
        "[providers.mapillary]\nenabled = true\n\n"
        "[providers.mapillary_streets]\nenabled = true\n\n"
        "[hosts.mapillary_tiles]\nrolling_24h_request_budget = 1200\n\n"
        f'[paths]\ndata_dir = "{data_dir}"\ndb_path = "{db_path}"\nlog_dir = "{log_dir}"\n'
    )
    yield {"conn": conn, "db_path": db_path, "log_dir": log_dir, "cfg": cfg, "data": data_dir}
    conn.close()


def _night(result, day):
    return next(n for n in result["nights"] if n["date"] == day)


def _run_report(prod, *args):
    import json

    out = prod["cfg"].parent / "report.json"
    code = report.main(
        ["--config", str(prod["cfg"]), "--since", "2026-10-15", "--until", "2026-10-27"]
        + ["--json", str(out), *args]
    )
    assert code == 0
    return json.loads(out.read_text())


def test_a_done_line_with_deferrals_holds_and_presence_parses_into_one_row():
    runs = {r.date: r for r in report.parse_scheduler_log([DONE_1016, DONE_1022, DONE_1025])}

    assert runs["2026-10-22"].pool_deferred == 20
    assert runs["2026-10-22"].presence_h == pytest.approx(1.1)
    assert runs["2026-10-22"].presence == "09:01-10:06"
    assert runs["2026-10-22"].gap_h is None, "the pull cut the gap off; never guess one"
    assert runs["2026-10-22"].cities == 80

    assert runs["2026-10-25"].held == "rest night (Sun)"
    assert runs["2026-10-25"].held_launches == 0
    assert runs["2026-10-25"].fill == "not reached"
    assert runs["2026-10-25"].presence_h is None, "Panoramax's presence is not Mapillary's"

    assert runs["2026-10-16"].refused and not runs["2026-10-16"].throttled
    assert runs["2026-10-16"].skipped_latched == 2
    assert runs["2026-10-16"].holding_mapillary == 3
    assert runs["2026-10-16"].fill_cities == 0
    assert all(r.done and not r.filtered for r in runs.values())


# NOT from the pull (it holds no stranded line): scheduler._collect_due's
# f-string, with reached >= promoted as the hoist guarantees (the promoted
# cities lead the slate, so they are inside the cap by construction).
STRANDED_1026 = (
    "2026-10-26 02:01:00,000 - streetscape_scheduler - INFO - 12 of 465 cities not due on "
    "gsv are inside tonight's 80-city cap ([schedule].opt_in_cities_per_day=10 reserved, "
    "10 promoted); 453 wait for a later night"
)


def _start(day, due=120, cap=80, filt=""):
    """A start line in cmd_run_due's format (NOT from the pull, which has none)."""
    return (
        f"{day} 02:00:05,000 - streetscape_scheduler - INFO - {due} cities due on {day}{filt}; "
        f"processing up to {cap} within daily budgets of 1,500 mapillary requests"
    )


def _done(day, cities, filt=""):
    """A minimal Done: line in cmd_run_due's format (NOT from the pull)."""
    return (
        f"{day} 04:00:00,000 - streetscape_scheduler - INFO - Done: run-due {day}{filt}: "
        f"{cities}/{cities} runs succeeded across {cities} cities in 1.00 h"
    )


class _Cfg:
    max_cities_per_day = 80


def test_the_events_before_a_done_line_belong_to_its_night():
    """The fill's room is quoted only on a decline; first and last, per night."""
    runs = report.parse_scheduler_log([DONE_1025, STRANDED_1026, *FILL_ROOM_1026, DONE_1026])
    night = runs[-1]
    assert night.date == "2026-10-26"
    assert night.fill_rooms == [69, 5]
    assert (night.stranded_reached, night.stranded_total) == (12, 465)
    assert runs[0].fill_rooms == [] and runs[0].stranded_total is None


def test_a_run_that_crashed_after_its_start_line_is_not_folded_into_the_next():
    """Review finding 4: a second start line closes the first run as crashed.

    Kills: overwriting ``pending.date`` on a start line (the 10-18 run then
    vanishes and its fill decline is charged to 10-19), and keeping a stranded
    line logged AFTER the dead run's start with the dead run (that line
    precedes its OWN run's start, so it is the next run's).
    """
    lines = [
        _start("2026-10-18"),
        FILL_ROOM_1026[0].replace("2026-10-26", "2026-10-18"),
        STRANDED_1026.replace("2026-10-26", "2026-10-19"),
        _start("2026-10-19"),
        _done("2026-10-19", 27),
    ]
    runs = report.parse_scheduler_log(lines)
    assert [(r.date, r.done, r.crashed) for r in runs] == [
        ("2026-10-18", False, True),
        ("2026-10-19", True, False),
    ]
    assert runs[0].fill_rooms == [69] and runs[1].fill_rooms == []
    assert runs[0].stranded_total is None
    assert (runs[1].stranded_reached, runs[1].stranded_total) == (12, 465)
    assert report._ended_by(runs[0], _Cfg()).startswith("crashed")


def test_the_0200_nightly_wins_over_an_unmarked_hand_run_on_the_same_date(prod):
    """Review finding 5: a bare ``--limit`` or a second full run is a hand run.

    ``--provider``/``--city`` carry a ``[--...]`` mark; ``--limit`` does not,
    so it is read off the start line's cap. Kills: taking the LAST unfiltered
    run of the date (the old ``nightly[-1]``), and dropping the narrowed check
    (the 3-city ``--limit`` run that comes first then becomes the night).
    """
    lines = [
        # Before 02:00 local, same UTC date: a bare --limit 3 catch-up.
        _start("2026-10-19", due=120, cap=3),
        _done("2026-10-19", 3),
        _start("2026-10-19", due=120, cap=80),
        _done("2026-10-19", 27),
        # Later that day, an unfiltered full hand run.
        _start("2026-10-19", due=93, cap=80),
        _done("2026-10-19", 5),
    ]
    (prod["log_dir"] / "streetscape_scheduler.log").write_text("\n".join(lines) + "\n")
    night = _night(_run_report(prod), "2026-10-19")
    assert night["nightly_run"]["cities"] == 27
    assert night["hand_runs"] == 2


def test_a_mapillary_refusal_is_read_when_another_host_refused_the_same_night():
    """Review finding 3: the verdict follows the LAST label of the run.

    ``_blocked_summary_note`` joins refused hosts with "; " and appends
    " unavailable" once; Mapillary's label sorts first. Kills: reading one
    ";"-segment at a time (both two-host lines then miss Mapillary), and
    dropping the clause-boundary lookbehind (the pool clause's "budget of
    <Mapillary label>" then joins a KartaView-only refusal).
    """
    head = (
        "2026-10-16 06:30:19,488 - streetscape_scheduler - INFO - Done: run-due 2026-10-16: "
        "34/34 runs succeeded across 20 cities (20 due, 0 fill) in 4.25 h"
    )
    mly = "Mapillary's tile CDN (tiles.mapillary.com)"
    kv = "the KartaView API (kartaview.org)"
    ovp = "the Overpass API (overpass-api.de)"

    def parse(rest):
        (run,) = report.parse_scheduler_log([head + rest])
        return run

    # Mapillary + KartaView, as the scheduler sorts and joins them.
    two = parse(f"; {mly}; {kv} unavailable, 2 launch(es) skipped while latched")
    assert two.refused and not two.throttled and two.skipped_latched == 2
    # The 2026-08-14 shape: Mapillary + Overpass.
    assert parse(f"; {mly}; {ovp} unavailable, 4 launch(es) skipped while latched").refused
    # Mapillary throttled while KartaView refused: the second part follows ", ".
    mixed = parse(f"; {kv} unavailable, {mly}; {ovp} THROTTLED (HTTP 429/503, no refusal")
    assert mixed.throttled and not mixed.refused
    # Only KartaView refused, after a Mapillary pool deferral: not Mapillary's.
    other = parse(
        f"; 20 deferred for the rolling-24h budget of {mly}; {kv} unavailable, "
        "1 launch(es) skipped while latched"
    )
    assert not other.refused and other.pool_deferred == 20


def test_a_night_still_in_flight_is_dated_from_its_start_line():
    start = (
        "2026-10-27 02:00:05,000 - streetscape_scheduler - INFO - 120 cities due on 2026-10-27; "
        "processing up to 80 within daily budgets of x requests"
    )
    runs = report.parse_scheduler_log([DONE_1026, start])
    assert [(r.date, r.done) for r in runs] == [("2026-10-26", True), ("2026-10-27", False)]


def test_mapillary_channels_cut_by_a_winddown_or_the_deadline_are_counted_for_375():
    lines = [
        "2026-10-21 10:40:00,000 - streetscape_scheduler - WARNING - a--b [mapillary]: child "
        "was killed by the stop signal (received SIGTERM) — not counted as a failure for this "
        "city.",
        "2026-10-21 10:41:00,000 - streetscape_scheduler - INFO - c--d: stop requested — not "
        "starting mapillary, mapillary_streets, panoramax. Not counted as failures; these "
        "channels keep their cadence and lead the next batch's queue.",
        "2026-10-21 10:42:02,044 - streetscape_scheduler - INFO - Done: run-due 2026-10-21: "
        "19/20 runs succeeded across 11 cities (1 due, 10 fill) in 8.66 h; 4 channel(s) "
        "deferred for the deadline (gsv 1, mapillary 2, mapillary_streets 1); fill (early "
        "refresh, >= 30 d): 10 cities, 1 realigned, 18/19 runs, 18 early refresh(es) recorded; "
        "declined 1 for deadline; ended by received SIGTERM; stopped early (received SIGTERM)",
    ]
    (run,) = report.parse_scheduler_log(lines)
    assert run.winddown_cut == 3
    assert run.deadline_deferred == 3
    assert run.stopped == "received SIGTERM"
    assert run.fill == "10 cities, ended by received SIGTERM"


def test_tile_status_lines_sum_per_night_across_child_logs(prod):
    for city, line in STATUS_1023.items():
        (prod["log_dir"] / f"collect_{city}_mapillary_2026-10-23.log").write_text(
            "2026-10-23 02:00:00 - x - INFO - starting\n" + line + "\n"
        )
    # A gsv child log on the same night is not a Mapillary crawl.
    (prod["log_dir"] / "collect_x_gsv_2026-10-23.log").write_text(
        STATUS_1023["bendigo--victoria--australia"] + "\n"
    )
    night = report.tile_statuses_by_night(str(prod["log_dir"]))["2026-10-23"]
    assert night["200"] == 251
    assert night["requests"] == 251
    assert (night["crawls"], night["logs"]) == (3, 3)
    assert night["429"] == night["5xx"] == night["3xx"] == 0


def test_a_night_with_no_status_line_reports_na_not_zero(prod):
    (prod["log_dir"] / "collect_a--b_mapillary_2026-10-24.log").write_text(
        "2026-10-24 02:10:00 - x - INFO - nothing about statuses here\n"
    )
    result = _run_report(prod)
    night = _night(result, "2026-10-24")
    assert night["tile_statuses"] is None
    assert night["mapillary_child_logs"] == 1
    rendered = report.render(result)
    row = next(line for line in rendered.splitlines() if line.startswith("2026-10-24"))
    assert "n/a (0/1)" in row
    assert result["stage1_clock"]["status_429_5xx"] is None, "no evidence is not zero 429s"


def test_per_process_requests_never_the_cumulative_total(prod):
    """10-24's resumed crawls: the runs row holds the sweep's cumulative total."""
    conn = prod["conn"]
    city = _register(conn, "Charleston")
    _run_row(conn, city, "2026-10-24", 778)
    db.add_api_usage(conn, date(2026, 10, 24), 372, "mapillary")
    conn.commit()
    night = _night(_run_report(prod), "2026-10-24")
    assert night["served"] == 372
    assert night["sweep_total"] == 778
    assert night["grid_runs"] == 1


def test_the_mapillary_only_due_population_excludes_gsv_due_cities(prod):
    conn = prod["conn"]
    only = [_register(conn, f"Mly{i}") for i in range(3)]
    paired = [_register(conn, f"Both{i}") for i in range(2)]
    for cid in only:
        for channel in ("gsv", "gsv_streets"):
            db.record_attempt(conn, cid, success=True, provider=channel)
    # The seed city is gsv-due too; make it fresh everywhere so it is neither.
    for channel in ("gsv", "gsv_streets", "mapillary", "mapillary_streets"):
        db.record_attempt(conn, "seed--oregon--united-states", success=True, provider=channel)
    conn.commit()
    pop = _run_report(prod)["mapillary_only_due"]
    assert pop["cities"] == 3
    assert sorted(pop["city_ids"]) == sorted(only)
    assert pop["also_due_on_gsv"] == len(paired)
    assert pop["grid_due"] == 3 and pop["walk_only_due"] == 0
    assert pop["priced_requests_upper_bound"] > 0


def test_the_catalog_is_opened_read_only_and_an_older_schema_is_refused(prod, capsys):
    conn = prod["conn"]
    conn.execute("PRAGMA user_version = 21")
    conn.commit()
    before = prod["db_path"].read_bytes()
    code = report.main(["--config", str(prod["cfg"]), "--since", "2026-10-15"])
    assert code == 2
    assert "schema version 21" in capsys.readouterr().err
    check = sqlite3.connect(prod["db_path"])
    assert check.execute("PRAGMA user_version").fetchone()[0] == 21
    check.close()
    assert prod["db_path"].read_bytes() == before


def test_a_current_catalog_is_read_and_left_byte_identical(prod):
    conn = prod["conn"]
    db.add_api_usage(conn, date(2026, 10, 22), 1200, "mapillary")
    conn.commit()
    before = prod["db_path"].read_bytes()
    assert _night(_run_report(prod), "2026-10-22")["served"] == 1200
    assert prod["db_path"].read_bytes() == before


def test_it_never_lists_the_data_directory(prod, monkeypatch):
    seen = []
    real_glob, real_listdir, real_scandir = glob.glob, os.listdir, os.scandir

    def spy(fn):
        def inner(path=".", *a, **k):
            seen.append(str(path))
            return fn(path, *a, **k)

        return inner

    monkeypatch.setattr(glob, "glob", spy(real_glob))
    monkeypatch.setattr(os, "listdir", spy(real_listdir))
    monkeypatch.setattr(os, "scandir", spy(real_scandir))
    _run_report(prod)
    assert seen, "the spy saw nothing; the test would pass vacuously"
    assert not [p for p in seen if p.startswith(str(prod["data"]))]


_CLEAN = {"200": 155, "429": 0, "5xx": 0, "3xx": 0}


def _clock_night(served, presence_h, statuses=_CLEAN):
    return {
        "served": served,
        "nightly_run": {"presence_h": presence_h},
        "tile_statuses": dict(statuses) if statuses else None,
    }


def test_the_stage1_clock_counts_nights_at_the_pool_share_and_reports_presence():
    """Review finding 1: >= 75% of the pool, presence reported and NOT gated.

    10-22 served the whole pool in 1.1 h, and at 40/min the pool is ~30 min of
    fetching, so a presence floor kept every full night out. Kills:
    reinstating any presence floor above 1.1 h (the 1.1 h night stops
    counting and the 15-night window stops meeting), and dropping the pool
    share (the 300-request night counts).
    """
    nights = [
        _clock_night(1200, 1.1),
        _clock_night(950, 2.5),
        _clock_night(300, 3.0),
        _clock_night(0, None, None),
    ]
    clock = report._stage1_clock(nights, 1200, [])
    assert (clock["active_nights"], clock["design_nights"]) == (3, 2)
    assert clock["design_presence_h"] == [1.1, 2.5]
    assert clock["criterion_met"] is False, "3 active nights is under 15"
    many = [_clock_night(1200, 1.1)] * 15
    assert report._stage1_clock(many, 1200, [])["criterion_met"] is True
    refused = [{"verdict": "refused"}]
    assert report._stage1_clock(many, 1200, refused)["criterion_met"] is False
    warned = many[:-1] + [_clock_night(1200, 1.1, _CLEAN | {"429": 1})]
    assert report._stage1_clock(warned, 1200, [])["criterion_met"] is False


def test_the_stage1_clock_never_starts_on_missing_tile_status_evidence():
    """Review finding 2: absence of evidence is UNKNOWN, never a pass.

    Kills: summing over an empty ``seen`` as 0 (15 evidence-free nights then
    meet the criterion), and judging only the nights that have a status line
    (14 clean nights plus one with no line would meet it).
    """
    blind = [_clock_night(1000, 2.5, None)] * 15
    clock = report._stage1_clock(blind, 1200, [])
    assert clock["status_429_5xx"] is None
    assert clock["nights_without_status"] == 15
    assert clock["criterion_met"] is False

    partial = [_clock_night(1000, 2.5)] * 14 + [_clock_night(1000, 2.5, None)]
    clock = report._stage1_clock(partial, 1200, [])
    assert clock["status_429_5xx"] is None, "one blind night makes the sum unknown"
    assert clock["status_429_5xx_seen"] == 0
    assert clock["nights_without_status"] == 1
    assert clock["criterion_met"] is False
    # An inactive night needs no evidence: nothing was asked of the CDN.
    rested = [_clock_night(1000, 2.5)] * 15 + [_clock_night(0, None, None)]
    assert report._stage1_clock(rested, 1200, [])["criterion_met"] is True


def test_the_summary_says_unknown_when_a_night_has_no_status_line(prod):
    """The rendered clock names the blind nights instead of printing a 0."""
    db.add_api_usage(prod["conn"], date(2026, 10, 24), 372, "mapillary")
    prod["conn"].commit()
    rendered = report.render(_run_report(prod))
    line = next(x for x in rendered.splitlines() if x.startswith("Stage 1 clock"))
    assert "429/5xx n/a (UNKNOWN: 1 active night(s) with no status line" in line
    assert "criterion met: no" in line
