"""Sampling invariants for the night-length analysis (issue #240).

The script that will produce `docs/experiments/night-length_metrics.json` reads
the scheduler's own logs, and its hazards are all *attribution* hazards rather
than parsing ones: a night attributed to the wrong knob, or an incomparable
night pooled into the comparison, produces a speed-up number that is wrong
quietly and in the flattering direction. These tests pin the separations.
"""

from scripts import night_length_analyze as nla

# One synthetic log holding every shape the parser has to tell apart. Built by
# hand rather than trimmed from a prod log, so the expected answer is arithmetic
# rather than a re-derivation of whatever the file happened to contain.
_LOG = """\
2026-08-01 01:00:00,000 - streetscape_scheduler - INFO - 900 cities due on 2026-08-01; \
processing up to 20 within daily budgets of 10,000,000 gsv requests; max_concurrent_channels=1
2026-08-01 07:00:00,000 - streetscape_scheduler - INFO - Done: run-due 2026-08-01: 80/80 runs \
succeeded across 20 cities in 6.00 h
2026-08-02 01:00:00,000 - streetscape_scheduler - INFO - 900 cities due on 2026-08-02; \
processing up to 20 within daily budgets of 10,000,000 gsv requests; max_concurrent_channels=1
2026-08-02 09:00:00,000 - streetscape_scheduler - INFO - Done: run-due 2026-08-02: 76/80 runs \
succeeded across 20 cities in 8.00 h; 3 deferred for budget
2026-08-03 01:00:00,000 - streetscape_scheduler - INFO - 900 cities due on 2026-08-03 \
[--provider mapillary]; processing up to 40 within daily budgets of 1,750 mapillary requests; \
max_concurrent_channels=1
2026-08-03 02:00:00,000 - streetscape_scheduler - INFO - Done: run-due 2026-08-03 \
[--provider mapillary]: 40/40 runs succeeded across 40 cities in 1.00 h
2026-08-04 01:00:00,000 - streetscape_scheduler - INFO - 900 cities due on 2026-08-04; \
processing up to 20 within daily budgets of 10,000,000 gsv requests; max_concurrent_channels=2
2026-08-04 11:00:00,000 - streetscape_scheduler - INFO - Done: run-due 2026-08-04: 44/48 runs \
succeeded across 12 cities in 10.00 h; stopped early (batch deadline reached (10 h); 8 due \
cities not attempted)
2026-08-05 01:00:00,000 - streetscape_scheduler - INFO - 900 cities due on 2026-08-05; \
processing up to 20 within daily budgets of 10,000,000 gsv requests; max_concurrent_channels=2
2026-08-05 04:00:00,000 - streetscape_scheduler - INFO - Done: run-due 2026-08-05: 80/80 runs \
succeeded across 20 cities in 3.00 h; 2 channel(s) skipped, the Mapillary tile CDN busy locally
2026-08-06 07:00:00,000 - streetscape_scheduler - INFO - Done: run-due 2026-08-06: 80/80 runs \
succeeded across 20 cities in 4.00 h
"""


def test_the_knob_comes_from_the_nights_own_start_line():
    """A night's setting must be recoverable from that night's record.

    The whole measurement is a comparison ACROSS a config flip, so which side of
    it a night ran on cannot rest on an operator's memory of the flip date.
    """
    nights = nla.parse_log(_LOG)
    assert [(n["date"], n["knob"]) for n in nights] == [
        ("2026-08-01", 1),
        ("2026-08-02", 1),
        ("2026-08-03", 1),
        ("2026-08-04", 2),
        ("2026-08-05", 2),
        ("2026-08-06", None),
    ]


def test_a_night_with_no_start_line_is_unknown_rather_than_assumed_sequential():
    """The last night above has a `Done:` line and no start line — a run whose
    opening rotated out, or a pre-#240 night that never logged the key.

    Defaulting it to 1 would pool the ENTIRE pre-#240 corpus into the control
    group, inventing a large, favourable sample for the side of the comparison
    that has no evidence at all.
    """
    nights = nla.parse_log(_LOG)
    orphan = nights[-1]
    assert orphan["knob"] is None and orphan["due"] is None
    summary = nla.summarize(nights, usage={})
    assert summary["population_counts"]["knob_unknown"] == 1
    assert all("2026-08-06" not in data["dates"] for data in summary["by_knob"].values()), (
        "a night of unknown provenance must not appear under any knob"
    )


def test_filtered_and_truncated_nights_are_counted_but_never_compared():
    """Two nights are short for reasons that have nothing to do with lanes: a
    `--provider` catch-up runs a subset of channels, and a deadline-stopped night
    reports the cap rather than the work.

    Both would flatter the comparison — the catch-up is the shortest night in the
    file and the truncated one is pinned at exactly max_batch_hours — so both are
    excluded from the distribution and reported by count, because a silent
    exclusion reads as coverage the analysis never had.
    """
    nights = nla.parse_log(_LOG)
    summary = nla.summarize(nights, usage={})
    assert summary["population_counts"] == {
        # `full` counts the unfiltered, un-truncated nights; `comparable` is the
        # subset of those whose knob is known, so the two differ by exactly the
        # orphan night above — which is why both numbers are reported.
        "full": 4,
        "filtered": 1,
        "truncated": 1,
        "knob_unknown": 1,
        "comparable": 3,
    }
    assert summary["by_knob"]["1"]["elapsed_hours"]["n"] == 2
    assert summary["by_knob"]["1"]["dates"] == ["2026-08-01", "2026-08-02"]
    # 2026-08-04 is the knob-2 night that hit the deadline; the only knob-2 night
    # that counts is 08-05, and 08-06's unknown knob keeps it out entirely.
    assert summary["by_knob"]["2"]["dates"] == ["2026-08-05"]


def test_hours_per_city_is_reported_beside_hours_not_instead_of_it():
    """A night's length is dominated by which cities came due, so the headline
    number needs the per-city figure next to it — and the raw hours stay, because
    that is the thing an operator actually experiences."""
    summary = nla.summarize(nla.parse_log(_LOG), usage={})
    knob1 = summary["by_knob"]["1"]
    assert knob1["elapsed_hours"]["p50"] == 7.0  # 6.00 and 8.00 h
    assert knob1["hours_per_city"]["p50"] == 0.35  # both nights ran 20 cities
    assert summary["by_knob"]["2"]["elapsed_hours"]["max"] == 3.0


def test_the_volume_control_and_the_busy_counter_survive_into_the_summary():
    """The negative controls are the point, not decoration.

    Lanes must compress wall clock and leave request volume alone, and a busy
    host-lock skip on a night with no manual run means our own lanes raced for a
    per-IP host — a hole in the affinity gating, which is stop-the-line.
    """
    usage = {
        "2026-08-01": {"gsv": 500_000, "mapillary": 1_100},
        "2026-08-02": {"gsv": 480_000, "mapillary": 1_050},
        "2026-08-05": {"gsv": 505_000, "mapillary": 1_080},
    }
    summary = nla.summarize(nla.parse_log(_LOG), usage=usage)
    knob1 = summary["by_knob"]["1"]["requests_by_channel"]
    assert knob1 == {
        "dates_with_usage": 2,
        "total_requests": {"gsv": 980_000, "mapillary": 2_150},
    }
    assert summary["by_knob"]["2"]["requests_by_channel"]["dates_with_usage"] == 1
    assert summary["by_knob"]["1"]["busy_channel_skips"] == 0
    assert summary["by_knob"]["2"]["busy_channel_skips"] == 2


def test_a_percentile_is_never_reported_without_its_n():
    """A shape quoted without the sample size behind it is how a two-night
    speed-up gets read as a measured result."""
    assert nla.describe([]) == {"n": 0}
    described = nla.describe([1.0, 2.0, 3.0])
    assert described["n"] == 3
    assert described["min"] == 1.0 and described["max"] == 3.0
    assert described["p50"] == 2.0


def test_two_run_dues_on_one_date_are_two_nights_but_one_days_request_volume():
    """The volume control must not double-count a date that ran twice.

    An incident night is exactly this shape — a nightly, a crash, a re-run — and
    `api_usage` is keyed by (date, provider) and already holds the whole day's
    total. Adding it once per night inflates the lane side of the one comparison
    the script exists to make, and docs/provider-access.md calls a lane night
    that spent MORE than a sequential one stop-the-line. Elapsed hours still
    count both, because two nights really are two observations of wall clock.
    """
    log = """\
2026-09-01 01:00:00,000 - streetscape_scheduler - INFO - 900 cities due on 2026-09-01; \
processing up to 20 within daily budgets of 10,000,000 gsv requests; max_concurrent_channels=3
2026-09-01 03:00:00,000 - streetscape_scheduler - INFO - Done: run-due 2026-09-01: 40/40 runs \
succeeded across 10 cities in 2.00 h
2026-09-01 04:00:00,000 - streetscape_scheduler - INFO - 900 cities due on 2026-09-01; \
processing up to 20 within daily budgets of 10,000,000 gsv requests; max_concurrent_channels=3
2026-09-01 07:00:00,000 - streetscape_scheduler - INFO - Done: run-due 2026-09-01: 40/40 runs \
succeeded across 10 cities in 3.00 h
"""
    usage = {"2026-09-01": {"gsv": 100_000, "mapillary": 1_500}}
    summary = nla.summarize(nla.parse_log(log), usage=usage)
    knob3 = summary["by_knob"]["3"]

    assert knob3["elapsed_hours"]["n"] == 2, "two nights are two elapsed observations"
    assert knob3["requests_by_channel"] == {
        "dates_with_usage": 1,
        "total_requests": {"gsv": 100_000, "mapillary": 1_500},
    }, "one date's api_usage row is one day's volume, however many run-dues touched it"


def test_a_driving_plan_error_saying_unavailable_is_not_a_per_ip_refusal():
    """`nights_with_a_host_refusal` is the signal that drops the knob to 1.

    The summary tail carries the blocked-host note, the stop reason AND the
    driving-plan error, so a bare substring search for "unavailable" reports a
    per-IP refusal on any night whose plan fetch got a 503. That is a false
    positive on a stop-the-line indicator; the real note is its own `; ` segment
    ending in " unavailable".
    """
    log = """\
2026-09-02 01:00:00,000 - streetscape_scheduler - INFO - 900 cities due on 2026-09-02; \
processing up to 20 within daily budgets of 10,000,000 gsv requests; max_concurrent_channels=2
2026-09-02 05:00:00,000 - streetscape_scheduler - INFO - Done: run-due 2026-09-02: 80/80 runs \
succeeded across 20 cities in 4.00 h; driving-plan fetch failed: HTTP 503 Service Unavailable
2026-09-03 01:00:00,000 - streetscape_scheduler - INFO - 900 cities due on 2026-09-03; \
processing up to 20 within daily budgets of 10,000,000 gsv requests; max_concurrent_channels=2
2026-09-03 05:00:00,000 - streetscape_scheduler - INFO - Done: run-due 2026-09-03: 60/80 runs \
succeeded across 20 cities in 4.00 h; the Overpass API (overpass-api.de) unavailable
"""
    nights = {n["date"]: n["hosts_unavailable"] for n in nla.parse_log(log)}
    assert nights == {"2026-09-02": False, "2026-09-03": True}
    assert (
        nla.summarize(nla.parse_log(log), usage={})["by_knob"]["2"]["nights_with_a_host_refusal"]
        == 1
    )


def test_two_blocked_hosts_still_read_as_one_refusal_note():
    """The note joins its labels with `; ` too, so only its LAST segment ends in
    " unavailable" — the parser must not require the first one to."""
    rest = (
        "; Mapillary's tile CDN (tiles.mapillary.com); "
        "the Overpass API (overpass-api.de) unavailable; stopped early (SIGTERM)"
    )
    assert nla._hosts_unavailable(rest)
