"""The systemd units that run the post-batch prefreeze pass (issues #355, #389).

`scripts/prefreeze_street_networks.py` shipped in #343 with no timer, so the one
#341 follow-up that takes Overpass out of the night never ran. These tests pin
what makes the units do their job rather than merely exist: they run on the
same host, interpreter, config and lock directory as the nightly batch; the
command line really fetches, really alerts and parses under the script's own
parser; the pacing stays inside the Overpass usage policy's regular-application
figure; and the pass is chained to the collection unit's end on success AND on
failure, ends an hour before the next fire at the production max_batch_hours,
and predicts the date the next fire reads.

Every figure is read out of the files themselves (and the production TOML), so a
change on either side of a cross-file agreement fails here rather than on prod.
"""

import glob
import os
import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from scripts import prefreeze_street_networks as pf  # noqa: E402
from streetscape_metadata_tracker import catalog_backup, scheduler  # noqa: E402
from streetscape_metadata_tracker.scheduler import load_scheduler_config  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
UNIT_DIR = ROOT / "deploy" / "systemd"
PROD_CONFIG = ROOT / "config" / "scheduler.makelab1.toml"
# How the units spell the checkout; substituted to resolve paths in this repo.
UNIT_CHECKOUT = "%h/streetscape-tracker"

# The Overpass public instance's figure for a REGULAR application: "divide those
# numbers by 100 (making less than 100 queries fetching less 10 MB of data per
# day fine)" -- wiki.openstreetmap.org/wiki/Overpass_API, read 2026-09-22.
OVERPASS_REGULAR_QUERIES_PER_DAY = 100
# The most cities a production night has run, measured under the old 40-city
# cap (docs/scheduler.md). Re-measure once nights are deadline-governed.
NIGHT_REACH_CITIES = 40

# alerting._send_smtp builds its connection with timeout=30, and a relay can
# spend that per stage (connect, STARTTLS, login, send).
SMTP_STAGE_TIMEOUT_S = 30


def _parse_unit(name: str) -> dict[str, dict[str, list[str]]]:
    """{section: {key: [values, in order]}}. systemd repeats keys (ReadWritePaths),
    which configparser cannot represent, and comments are not directives."""
    return _parse_unit_text((UNIT_DIR / name).read_text())


def _parse_unit_text(text: str) -> dict[str, dict[str, list[str]]]:
    """``_parse_unit`` over a unit's text rather than its file name."""
    sections: dict[str, dict[str, list[str]]] = {}
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
            continue
        key, _, value = line.partition("=")
        current.setdefault(key.strip(), []).append(value.strip())
    return sections


def _one(unit, section, key):
    values = unit.get(section, {}).get(key, [])
    assert len(values) == 1, f"expected exactly one {key}= in [{section}], got {values}"
    return values[0]


def _env(unit) -> dict[str, str]:
    return dict(v.split("=", 1) for v in unit["Service"].get("Environment", []))


def _span_minutes(value: str) -> float:
    """A systemd time span in minutes; only the spellings these units use."""
    m = re.fullmatch(r"(\d+)\s*(s|sec|m|min|h)?", value)
    assert m, f"unrecognized time span {value!r}"
    return (
        int(m.group(1))
        * {None: 1 / 60, "s": 1 / 60, "sec": 1 / 60, "m": 1, "min": 1, "h": 60}[m.group(2)]
    )


def _bytes(value: str) -> int:
    """A systemd memory size in bytes; only the spellings these units use."""
    m = re.fullmatch(r"(\d+)([KMG])?", value)
    assert m, f"unrecognized size {value!r}"
    return int(m.group(1)) * {None: 1, "K": 2**10, "M": 2**20, "G": 2**30}[m.group(2)]


def _daily_pacific(timer) -> tuple[int, str]:
    """(minutes after local midnight, tz) of a daily `*-*-* HH:MM:SS Zone` OnCalendar."""
    spec = _one(timer, "Timer", "OnCalendar")
    m = re.fullmatch(r"\*-\*-\* (\d\d):(\d\d):(\d\d) (\S+)", spec)
    assert m, f"expected a daily calendar spec with an explicit zone, got {spec!r}"
    return int(m.group(1)) * 60 + int(m.group(2)), m.group(4)


def _exec_argv(unit) -> list[str]:
    return shlex.split(_one(unit, "Service", "ExecStart"))


def _shipped_units() -> list[str]:
    """Every unit file in deploy/systemd, by the one definition both sweeps use."""
    return sorted(glob.glob(str(UNIT_DIR / "*.service")) + glob.glob(str(UNIT_DIR / "*.timer")))


def _in_repo(path: str) -> Path:
    assert path.startswith(UNIT_CHECKOUT + "/"), path
    return ROOT / path[len(UNIT_CHECKOUT) + 1 :]


@pytest.fixture(scope="module")
def units():
    return {
        "service": _parse_unit("streetscape-prefreeze.service"),
        "collect": _parse_unit("streetscape-tracker.service"),
        "nightly": _parse_unit("streetscape-tracker.timer"),
    }


@pytest.fixture(scope="module")
def prod_cfg():
    return load_scheduler_config(str(PROD_CONFIG))


@pytest.fixture(scope="module")
def args(units):
    argv = _exec_argv(units["service"])
    return pf.build_parser().parse_args(argv[2:])


def test_it_runs_where_and_as_the_nightly_batch_does(units):
    """Host, interpreter, config, lock dir and log: each must equal the
    collection unit's, because each is shared state the night reads -- the
    catalog and osm_cache on one host, and an Overpass lock that only serializes
    two processes that derive the SAME path."""
    svc, collect = units["service"], units["collect"]
    assert _one(svc, "Unit", "ConditionHost") == _one(collect, "Unit", "ConditionHost")

    ours, theirs = _exec_argv(svc), _exec_argv(collect)
    assert ours[0] == theirs[0], "same interpreter as the nightly"
    assert ours[ours.index("--config") + 1] == theirs[theirs.index("--config") + 1]

    lock_dir = _env(svc).get("STREETSCAPE_LOCK_DIR")
    assert lock_dir and lock_dir == _env(collect)["STREETSCAPE_LOCK_DIR"], (
        "a different lock dir makes host_lock(HOST_OVERPASS) a no-op between this "
        "pass and the night -- two Overpass talkers from one IP"
    )
    assert _one(svc, "Service", "StandardOutput") == _one(collect, "Service", "StandardOutput")


def test_the_sandbox_is_the_checkout_and_nothing_wider(units):
    """Each directive named here, not just the two that shape the filesystem:
    dropping any one of them silently widens the unit, and the doc claim is
    about the sandbox as a whole."""
    svc, collect = units["service"], units["collect"]
    for directive in ("PrivateUsers", "NoNewPrivileges", "PrivateTmp", "RestrictSUIDSGID"):
        assert _one(svc, "Service", directive) == "true", directive
    assert _one(svc, "Service", "ProtectSystem") == "strict"
    rw = svc["Service"]["ReadWritePaths"]
    # The checkout (osm_cache, the catalog, locks/) and nothing else: it never publishes.
    assert rw == [collect["Service"]["ReadWritePaths"][0]]
    assert _env(svc)["STREETSCAPE_LOCK_DIR"].startswith(rw[0] + "/")
    # Optional (leading '-'): Overpass needs no credential, so a missing .env
    # must not fail the unit -- but [alerts] SMTP settings may live there, so it
    # is still read. Required, it would fail every pass on a host without one.
    env_file = _one(svc, "Service", "EnvironmentFile")
    assert env_file.startswith("-") and env_file.endswith("/.env")


def test_the_memory_cap_is_a_daytime_cap_not_the_nightly_one(units):
    """The VALUE, not merely the presence. Both directions bite: too low and the
    pass is OOM-killed -- a SIGKILL, so the --alert path never runs and this is
    the one failure that IS silent, repeating daily while the same oversized
    city heads the plan; as large as the nightly's, and a pass running in the
    working day can squeeze the co-tenants this host serves NFS to."""
    svc, collect = units["service"], units["collect"]
    ours = _bytes(_one(svc, "Service", "MemoryMax"))
    nightly = _bytes(_one(collect, "Service", "MemoryMax"))
    assert 8 * 2**30 < ours < nightly
    assert "MemoryHigh" not in svc["Service"], (
        "a soft brake turns an oversized graph into hours of silent reclaim (#157)"
    )


def test_the_stop_timeout_outlives_the_alert_a_sigterm_triggers(units):
    """TimeoutStartSec ends a slow pass with SIGTERM, and the handler's whole job
    is to send one mail. systemd's default 90 s can be outlasted by an SMTP relay
    timing out stage by stage (30 s each in alerting._send_smtp), which would
    SIGKILL the mail that says the pass died."""
    stop_s = _span_minutes(_one(units["service"], "Service", "TimeoutStopSec")) * 60
    assert stop_s >= 4 * SMTP_STAGE_TIMEOUT_S
    assert stop_s < _span_minutes(_one(units["service"], "Service", "TimeoutStartSec")) * 60


def test_the_command_line_fetches_alerts_and_parses(units, args):
    """A typo'd flag is argparse exit 2 after every night; a missing --execute is
    a dry run that looks healthy forever; a missing --alert is a pass that can
    die with nobody told. Parsed with the script's OWN parser, so the unit cannot
    name a flag the script does not have."""
    argv = _exec_argv(units["service"])
    assert _in_repo(argv[1]) == ROOT / "scripts" / "prefreeze_street_networks.py"
    assert _in_repo(argv[1]).is_file()
    assert _in_repo(args.config) == PROD_CONFIG and PROD_CONFIG.is_file()
    assert args.execute is True
    assert args.alert is True
    assert args.force is False, "--force would fetch beside an in-flight run-due"
    assert args.date is None, "a pinned --date would predict the same night forever"


def test_the_pass_covers_tonight_and_stays_inside_overpass_policy(args, prod_cfg):
    # Two nights, per the script's own reasoning: a night reaches past its cap.
    assert args.nights >= 2
    # Tonight's whole slate fits (the plan is in slate order, tonight first)...
    # Until 2026-09-25 this was pinned against max_cities_per_day. The cap then
    # began a staged raise (40 -> 80 first) toward max_batch_hours ending the
    # night, so the cap is no longer a night's reach, and at ~100 it would also
    # break the Overpass bound below. 40 is the most cities any night has run,
    # so it is the figure tonight's head is measured against until a
    # deadline-governed night shows otherwise.
    assert args.limit is not None and args.limit >= NIGHT_REACH_CITIES
    # ...and one pass stays under the regular-application daily figure, with room
    # for the night's own fetches of whatever the prediction missed.
    assert args.limit < OVERPASS_REGULAR_QUERIES_PER_DAY
    assert args.pause_s >= pf.DEFAULT_PAUSE_S


def _night_done(units, prod_cfg) -> float:
    """Minutes after local midnight by which the night has certainly ended.

    The nightly fires by 02:00 + its randomized delay and stops launching at
    max_batch_hours; then its tail runs -- backup, aggregate + manifest, publish.
    Only two of those three terms are importable constants, so the bound used
    here is the collection unit's TimeoutStopSec: a number in a FILE that was
    sized against those same components, and the more conservative of the two
    (30 min against their ~27 min sum). A stop is NOT involved in a normal
    night; it is a stand-in, and the assertion here keeps it an over-estimate."""
    stop_min = _span_minutes(_one(units["collect"], "Service", "TimeoutStopSec"))
    # The stand-in must stay an over-estimate of the tail terms that ARE
    # importable, or it has stopped standing in for anything.
    assert stop_min * 60 >= catalog_backup.BACKUP_TIMEOUT_S + scheduler.PUBLISH_TIMEOUT_S
    night_start, _ = _daily_pacific(units["nightly"])
    return (
        night_start
        + _span_minutes(_one(units["nightly"], "Timer", "RandomizedDelaySec"))
        + prod_cfg.max_batch_hours * 60
        + stop_min
    )


def test_the_weekly_screen_fires_after_the_night_and_ends_before_the_next(units, prod_cfg):
    """The Panoramax screen shares the nightly's Panoramax host lock, so an
    overlap is a screen that exits 85 and records nothing that week. It fired at
    Monday 12:00 until 2026-09-25 and 18:00 until #389, each inside a night once
    max_batch_hours grew. Both ends are pinned: it starts after the night's
    latest end, and its latest end (start + randomized delay + TimeoutStartSec)
    is an hour clear of the next 02:00. At 23:00 the first assert fails once
    max_batch_hours passes ~20.25 -- the signal to move or chain the screen."""
    timer = _parse_unit("streetscape-screen-provider.timer")
    service = _parse_unit("streetscape-screen-provider.service")
    spec = _one(timer, "Timer", "OnCalendar")
    m = re.fullmatch(r"Mon \*-\*-\* (\d\d):(\d\d):(\d\d) (\S+)", spec)
    assert m, f"expected a weekly Monday spec with an explicit zone, got {spec!r}"
    night_start, night_tz = _daily_pacific(units["nightly"])
    assert m.group(4) == night_tz == "America/Los_Angeles"
    night_done = _night_done(units, prod_cfg)
    screen_start = int(m.group(1)) * 60 + int(m.group(2))
    assert screen_start >= night_done, (
        f"the screen fires at minute {screen_start}, but the night can run to minute "
        f"{night_done:.0f} at max_batch_hours = {prod_cfg.max_batch_hours}"
    )
    screen_end = (
        screen_start
        + _span_minutes(_one(timer, "Timer", "RandomizedDelaySec"))
        + _span_minutes(_one(service, "Service", "TimeoutStartSec"))
    )
    assert screen_end <= 24 * 60 + night_start - 60, (
        f"a slow screen can still hold the Panoramax lock at minute {screen_end:.0f}, "
        "within an hour of the next nightly fire"
    )


def test_the_collection_unit_chains_the_pass_on_success_and_on_failure(units):
    """run-due exits nonzero on ANY failed city, so most nights end 'failed':
    without OnFailure= the pass would run only after a perfectly clean night,
    and without OnSuccess= only after an imperfect one. The chain must name a
    unit that ships."""
    collect = units["collect"]["Unit"]
    assert "streetscape-prefreeze.service" in collect.get("OnSuccess", [])
    assert "streetscape-prefreeze.service" in collect.get("OnFailure", [])
    assert (UNIT_DIR / "streetscape-prefreeze.service").is_file()


def test_the_opt_in_failure_notice_coexists_with_the_chain():
    """The collection unit ships the raw failure notice COMMENTED (opt-in;
    the scheduler's own [alerts] mail is the primary signal). An operator who
    uncomments it must get BOTH OnFailure= targets -- systemd accumulates the
    key -- so the chain must stay a separate line, and the notice line must
    still be there to uncomment."""
    text = (UNIT_DIR / "streetscape-tracker.service").read_text()
    notice = "#OnFailure=streetscape-tracker-notify@%n.service"
    assert notice in text.splitlines(), "the opt-in failure notice line is gone"
    sections = _parse_unit_text(
        "\n".join(line[1:] if line == notice else line for line in text.splitlines())
    )
    assert sorted(sections["Unit"]["OnFailure"]) == [
        "streetscape-prefreeze.service",
        "streetscape-tracker-notify@%n.service",
    ]
    assert (UNIT_DIR / "streetscape-tracker-notify@.service").is_file()


def test_no_timer_ships_for_the_prefreeze(units):
    """A timer beside the chain is two passes a day, two alert surfaces, and a
    wall-clock slot that re-breaks at every max_batch_hours raise -- the
    inheritance #389 removed. The service is triggered, never enabled, so it
    has no [Install] section either."""
    assert not (UNIT_DIR / "streetscape-prefreeze.timer").exists()
    assert "Install" not in units["service"]


def test_a_chained_pass_ends_well_before_the_next_night(units, prod_cfg):
    """TimeoutStartSec is the only thing between a pathological pass and the
    02:00 batch: a pass still holding the Overpass lock then makes the night's
    first cold walk exit busy and strand its city (#341). The chained pass
    starts no later than the night's latest end, so that plus its timeout must
    leave an hour before the next fire. Holds at 12 h (20:45) and 16 h (00:45);
    FAILS at 20 h by design -- that raise must shrink this timeout or --limit."""
    night_start, _ = _daily_pacific(units["nightly"])
    latest_end = _night_done(units, prod_cfg) + _span_minutes(
        _one(units["service"], "Service", "TimeoutStartSec")
    )
    assert latest_end <= 24 * 60 + night_start - 60, (
        f"at max_batch_hours = {prod_cfg.max_batch_hours} a chained pass can run to minute "
        f"{latest_end:.0f}, within an hour of the next fire: re-derive the prefreeze "
        "service's TimeoutStartSec (or its --limit) before this raise (#389)"
    )


def test_the_scripts_fire_time_is_the_nightly_timers(units):
    """next_run_date predicts the date the next 02:00 Pacific fire reads from
    constants restated in the script; a timer moved without them is a pass
    predicting the wrong night."""
    minutes, tz = _daily_pacific(units["nightly"])
    assert (pf.NIGHTLY_FIRE_HOUR * 60 + pf.NIGHTLY_FIRE_MINUTE, pf.NIGHTLY_FIRE_ZONE) == (
        minutes,
        tz,
    )


def test_every_unit_is_installed_by_the_deploy_readme():
    """A unit with no `cp` line in deploy/README.md is a unit nobody installs --
    which is how the prefreeze script sat unscheduled after #343."""
    readme = (ROOT / "deploy" / "README.md").read_text()
    installed: set[str] = set()
    for m in re.finditer(r"cp deploy/systemd/(\S+)", readme):
        spec = m.group(1)
        brace = re.fullmatch(r"(.*)\{(.+)\}", spec)
        if brace:
            installed.update(brace.group(1) + ext for ext in brace.group(2).split(","))
        else:
            installed.add(spec)
    shipped = {os.path.basename(p) for p in _shipped_units()}
    assert shipped - installed == set(), "units with no install step in deploy/README.md"


def test_claude_md_counts_the_units_that_ship():
    shipped = len(_shipped_units())
    text = (ROOT / "CLAUDE.md").read_text()
    m = re.search(r"Deployment lives in `deploy/` \((\d+) systemd units", text)
    assert m, "CLAUDE.md no longer states the unit count"
    assert int(m.group(1)) == shipped
