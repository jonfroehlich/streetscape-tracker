"""Sampling invariants for the publish-duration analysis (issues #218, #230).

The script that produces `docs/experiments/publish-duration_metrics.json` reads
the scheduler's own logs, and every hazard it has is a *pooling* hazard: three
populations sit interleaved in one file, and any two of them merged produce a
number that is wrong quietly. These tests pin the separations, not the parsing.
"""

import json
import re

import pytest

from scripts import publish_duration_analyze as pda

# One synthetic log holding every shape the parser has to tell apart. Built by
# hand rather than trimmed from a prod log so the expected answer is arithmetic,
# not a re-derivation of whatever the file happened to contain.
_LOG = """\
2026-08-01 01:00:00,000 - streetscape_scheduler - INFO - Publishing via /x/sync_data_to_server.sh
2026-08-01 01:00:12,500 - streetscape_scheduler - INFO - Published in 12.4 s
2026-08-02 02:00:00,000 - streetscape_scheduler - INFO - Publishing via /x/sync.sh --local
2026-08-02 02:00:20,000 - streetscape_metadata_tracker.alerting - INFO - Alert emailed to a@b
2026-08-03 03:00:00,000 - streetscape_scheduler - INFO - Publishing via /x/sync.sh --local
2026-08-03 03:00:00,050 - streetscape_scheduler - ERROR - Publish script failed
2026-08-04 04:00:00,000 - streetscape_scheduler - INFO - Publishing via /x/sync.sh
2026-08-04 04:00:03,200 - streetscape_scheduler - ERROR - Publish script failed (exit 12) after 3.2 s
2026-08-05 05:00:00,000 - streetscape_scheduler - INFO - Publishing via /x/sync.sh
2026-08-05 09:30:00,000 - streetscape_scheduler - INFO - 900 cities due on 2026-08-06; processing
2026-08-06 06:00:00,000 - streetscape_scheduler - INFO - Publishing via /x/sync.sh
"""


@pytest.fixture
def parsed():
    return pda.parse_log(_LOG)


def test_exact_and_bound_observations_are_never_pooled(parsed):
    """`Published in N.N s` is a measurement; `Publishing via` -> next line is an
    upper bound that also contains whatever ran in between (on the nights that
    have a successor at all, the alert's SMTP send). Pooling them publishes a
    distribution whose members do not measure the same quantity — and the bound
    is the one that would dominate, since it is the only population that exists
    before #229 is deployed."""
    assert [o["seconds"] for o in parsed["exact"]] == [12.4]
    assert [o["seconds"] for o in parsed["bound"]] == [20.0]
    assert all(o.get("upper_bound") for o in parsed["bound"]), (
        "a bound must carry the flag that says it is one; an unlabelled bound "
        "is indistinguishable from a measurement to every later reader"
    )
    assert all("upper_bound" not in o for o in parsed["exact"])


def test_a_failed_publish_never_enters_the_healthy_distribution(parsed):
    """A failure is not a publish duration. The prod history holds three that
    took 0.05-0.30 s (a bad --local/SSH mode, an immediate rsync exit); pooled
    into the healthy set they drop p25 by more than half and make any bound
    derived from the result look safer than it is."""
    assert len(parsed["failed"]) == 2
    healthy = [o["seconds"] for o in parsed["exact"] + parsed["bound"]]
    assert 3.2 not in healthy and 0.05 not in healthy

    summary = pda.summarize(parsed)
    assert summary["failed"]["overall"]["n"] == 1, "only the timed failure is timed"
    assert summary["failed"]["untimed"] == 1


def test_the_pre_229_bare_failure_line_is_not_read_as_a_fast_publish(parsed):
    """The sharpest version of the trap above, and the one that actually fires.

    Before #229 the failure line carried no elapsed, so it is simply the next
    line after `Publishing via` — 0.05 s later. The bound fallback would score
    that as a HEALTHY publish faster than any real one, becoming the new minimum
    of the distribution the timeout is sized from.
    """
    assert 0.05 not in [o["seconds"] for o in parsed["bound"]]
    untimed = [o for o in parsed["failed"] if o["seconds"] is None]
    assert len(untimed) == 1 and untimed[0]["at"].startswith("2026-08-03")


def test_a_successor_from_another_invocation_is_excluded_and_counted(parsed):
    """A healthy pre-#229 night logged NOTHING after `Publishing via` (that is
    #218's complaint), so its successor line — when the file has one — belongs to
    a later run hours away. Those must not become 16,200-second publishes.

    Counted rather than dropped: CLAUDE.md's "no silent caps" rule. A frame that
    quietly discards what it could not measure reads as coverage it never had,
    and here it would hide that most nights are unmeasurable at all.
    """
    reasons = [o["why"] for o in parsed["excluded"]]
    assert len(reasons) == 2
    assert any("later invocation" in r for r in reasons), "the 04:00 -> 09:30 pair"
    assert any("log ends here" in r for r in reasons), "the final Publishing via"
    assert pda.summarize(parsed)["excluded"]["n"] == 2


def test_the_two_publish_transports_are_reported_separately(parsed):
    """prod moved to `--local` in #215, so most of the history is over a
    transport prod no longer uses — an NFS copy and an rsync-over-SSH need not
    have the same shape, and a merged figure would let 14 SSH nights speak for
    the 2 that describe today."""
    assert {o["mode"] for o in parsed["exact"]} == {"ssh"}
    assert {o["mode"] for o in parsed["bound"]} == {"local"}
    assert pda.publish_mode("/x/sync.sh --local") == "local"
    assert pda.publish_mode("/x/sync.sh") == "ssh"


def test_percentiles_report_the_shape_not_just_a_median():
    """CLAUDE.md: the distribution is usually the finding, so a summary that is
    only a median hides it. Hand-computed against a vector whose quartiles fall
    between order statistics, since that is where an off-by-one interpolation
    would show."""
    stats = pda.percentiles([1.0, 2.0, 3.0, 4.0, 5.0])
    assert stats == {
        "n": 5,
        "min": 1.0,
        "p25": 2.0,
        "p50": 3.0,
        "p75": 4.0,
        "p90": 4.6,
        "p95": 4.8,
        "max": 5.0,
    }
    assert pda.percentiles([]) == {"n": 0}
    # Interpolated, matching numpy's default, so a later re-analysis in pandas
    # agrees with the committed record rather than landing a rung off.
    assert pda.percentiles([0.0, 10.0])["p25"] == 2.5


def test_generated_by_names_the_run_that_wrote_the_file():
    """A fixed constant would let `--docs-dir /tmp/scratch` write a record
    claiming the canonical provenance — a claim true of no run in particular,
    which is exactly what CLAUDE.md asks this field to rule out. The canonical
    invocation must still render DOCS_GENERATED_BY exactly."""
    assert (
        pda.docs_generated_by("logs", "docs/experiments", tree_walk=True) == pda.DOCS_GENERATED_BY
    )
    scratch = pda.docs_generated_by("/var/log/x", "/tmp/scratch", tree_walk=False)
    assert "/tmp/scratch" in scratch and "--time-tree-walk" not in scratch


def test_the_committed_record_still_matches_the_writeup():
    """The record and the prose are two copies of one measurement, and nothing
    else stops them drifting — the same reason the systemd unit's rationale is
    pinned to `_MEASURED_TAIL_AGGREGATE_S`. Every figure the writeup quotes is
    checked back against the JSON that produced it."""
    from pathlib import Path

    docs = Path(__file__).resolve().parent.parent / "docs" / "experiments"
    record = json.loads((docs / "publish-duration_metrics.json").read_text())
    prose = (docs / "publish-duration.md").read_text()

    # Thousands separators are a prose choice, not a value change, so compare
    # against a comma-stripped copy — otherwise this test quietly dictates how
    # the writeup may spell a number.
    flat = prose.replace(",", "")
    bound = record["summary"]["bound"]["overall"]
    for key in ("n", "p50", "p95", "max"):
        rendered = f"{bound[key]}" if key == "n" else f"{bound[key]:.1f}"
        assert rendered in flat, (
            f"the writeup no longer quotes bound {key}={rendered} from "
            f"publish-duration_metrics.json; regenerate both or fix the prose"
        )
    assert str(record["tree_walk"]["files_considered"]) in flat
    assert str(record["tree_walk"]["published_files"]) in flat
    assert str(record["tree_walk"]["published_gb"]) in flat
    # The walk is cache-sensitive by more than 10x, so the writeup quotes a RANGE
    # and both ends of it have to be the ones actually measured.
    walk = [record["tree_walk"]["as_found_seconds"], *record["tree_walk"]["warm_seconds"]]
    assert str(min(walk)) in flat and str(max(walk)) in flat, (
        "the writeup must quote both ends of the measured tree-walk range; one "
        "figure silently picks a side of a 10x spread"
    )

    # And the bound the whole study exists to justify has to be the one the code
    # actually uses — the number is quoted in three files and enforced in one.
    from streetscape_metadata_tracker import scheduler as sched

    assert re.search(rf"PUBLISH_TIMEOUT_S\D{{0,20}}{sched.PUBLISH_TIMEOUT_S:.0f}", prose), (
        "the writeup must quote the PUBLISH_TIMEOUT_S it argues for, and it must "
        "be the value scheduler.py holds"
    )
