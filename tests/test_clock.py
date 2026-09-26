"""
The one clock a snapshot is dated by (issue #347).

The census cache compares a marker's UTC ``completed_at`` date against the
consumer's ``run_date``, so every default snapshot date must come from the UTC
calendar. The road-walk collector read ``date.today()`` instead, and west of
UTC an evening walk was refused the census its own grid run had just promoted.
These tests pin the shared clock under a pinned Pacific zone and a frozen
Pacific-evening instant, where the two calendars disagree by a day.
"""

import io
import re
import tokenize
from pathlib import Path

from streetscape_metadata_tracker import clock, db
from tests.conftest import EVENING_LOCAL_DATE, EVENING_UTC, EVENING_UTC_DATE

REPO_ROOT = Path(__file__).resolve().parent.parent

# The modules on the collection path: everything that dates a snapshot, stamps
# a census cache marker, or keys the api_usage ledger a budget gate reads.
COLLECTION_PATH_MODULES = (
    "scripts/prefreeze_street_networks.py",
    "streetscape_metadata_tracker/checkpointing.py",
    "streetscape_metadata_tracker/cli.py",
    "streetscape_metadata_tracker/scheduler.py",
    "streetscape_street_analyzer/collect.py",
)

# A local-calendar read: `date.today()`, `datetime.today()`, or a naive
# `datetime.now()` with no tz argument.
_LOCAL_CALENDAR_RE = re.compile(r"\bdate\.today\(|\bdatetime\.today\(|\bdatetime\.now\(\s*\)")


def _code_lines(source: str) -> dict[int, str]:
    """
    The source's code with comments and string literals removed, by line.

    A comment or docstring that NAMES ``date.today()`` -- the #347 comment in
    collect.py does -- is not a read of the local calendar, so the grep must
    see code tokens only.

    An f-string is KEPT: its braces hold code (``f"publish_{date.today()}"``
    was one of the reads this replaced), and Python 3.11 tokenizes the whole
    f-string as one STRING token where 3.12+ splits out the expression.
    """
    lines: dict[int, list[str]] = {}
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE):
            continue
        if (
            tok.type == tokenize.STRING
            and "f" not in tok.string.split("'")[0].split('"')[0].lower()
        ):
            continue
        lines.setdefault(tok.start[0], []).append(tok.string)
    return {n: "".join(parts) for n, parts in lines.items()}


def test_snapshot_date_today_is_the_utc_calendar_date_not_the_local_one(
    pacific_local_zone, frozen_utc_clock
):
    frozen_utc_clock(EVENING_UTC)
    assert clock.snapshot_date_today() == EVENING_UTC_DATE
    assert clock.snapshot_date_today() != EVENING_LOCAL_DATE
    # The zone pin is live: the same instant read in local time is the day before.
    assert EVENING_UTC.astimezone().date() == EVENING_LOCAL_DATE


def test_db_utc_now_iso_reads_the_shared_clock(frozen_utc_clock):
    frozen_utc_clock(EVENING_UTC)
    assert db.utc_now_iso() == "2026-09-01T00:30:00+00:00"
    assert clock.utc_now_iso() == "2026-09-01T00:30:00+00:00"


def test_the_local_calendar_pattern_catches_what_it_claims():
    """Self-check, so the grep below cannot pass by matching nothing."""
    assert _LOCAL_CALENDAR_RE.search("x = date.today()")
    assert _LOCAL_CALENDAR_RE.search("x = datetime.today()")
    assert _LOCAL_CALENDAR_RE.search("x = datetime.now()")
    assert _LOCAL_CALENDAR_RE.search("x = datetime.now( )")
    assert not _LOCAL_CALENDAR_RE.search("datetime.now(UTC)")
    assert not _LOCAL_CALENDAR_RE.search("clock.snapshot_date_today()")
    # And the comment stripping keeps code while dropping prose.
    stripped = _code_lines('x = date.today()  # not date.today()\ny = "date.today()"\n')
    assert _LOCAL_CALENDAR_RE.search(stripped[1])
    assert not _LOCAL_CALENDAR_RE.search(stripped.get(2, ""))
    in_fstring = _code_lines('p = f"publish_{date.today().isoformat()}.log"\n')
    assert _LOCAL_CALENDAR_RE.search(in_fstring[1])


def test_no_collection_module_reads_a_local_calendar():
    offenders = []
    for rel in COLLECTION_PATH_MODULES:
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        for lineno, code in _code_lines(source).items():
            if _LOCAL_CALENDAR_RE.search(code):
                offenders.append(f"{rel}:{lineno}: {code}")
    assert offenders == [], (
        "A collection-path module reads the LOCAL calendar; date a snapshot with "
        "clock.snapshot_date_today() (UTC), or the census cache refuses an evening "
        "consumer's entry west of UTC (#347):\n" + "\n".join(offenders)
    )
